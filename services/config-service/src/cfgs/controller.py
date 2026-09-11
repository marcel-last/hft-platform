"""config_service — HTTP controller (request handlers).

The controller is a thin layer over the :class:`~cfgs.config_store.ConfigStore`.
It maps HTTP requests onto store operations, serializes failures using the
platform error envelope, and performs best-effort fan-out to S10 (alerting) and
S13 (audit) on every mutation.  All handlers are synchronous.

Endpoints implemented here (see dependency-map.json for S11):

    GET  /healthz              liveness probe
    GET  /readyz               readiness probe (any config stored yet?)
    GET  /configs              summary of all tracked services
    GET  /config/{service}     effective config (blob + env override merged)
    PUT  /config/{service}     create/update a service's config blob
    POST /reload               re-broadcast the current version table (no-op refresh)
    GET  /versions             version table (optionally ?service= for history)
    GET  /changes              change log (?since=&limit=)
    GET  /changes/watch        long-poll for changes (?since=&timeout_ms=&service=)
    POST /overrides            set an environment override
    DELETE /overrides          clear an environment override
    GET  /overrides            list overrides (optionally ?service=)
    GET  /flags                list all feature flags
    PUT  /flags/{name}         create/update a flag
    GET  /stats                store statistics
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .clients import AlertingClient, AuditClient
from .config import CONFIG
from .errors import CFGError, error_envelope
from .models import now_ns

logger = logging.getLogger("cfgs.controller")


class ConfigController:
    """Bundles the shared service state that handlers need."""

    def __init__(self) -> None:
        # wired up by main.py after construction
        self.store = None          # ConfigStore
        self.alerting = AlertingClient()
        self.audit = AuditClient()

    # ------------------------------------------------------------------
    # Health / readiness
    # ------------------------------------------------------------------

    def healthz(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok", "service": CONFIG.name, "version": CONFIG.version}

    def readyz(self) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        reasons = list(self.store.readiness_reasons())
        stats = self.store.stats_view()
        body = {
            "status": "ready" if not reasons else "not_ready",
            "reasons": reasons,
            "services_tracked": stats["services_tracked"],
            "change_events": stats["change_events"],
        }
        return (200 if not reasons else 503), body

    # ------------------------------------------------------------------
    # Config CRUD
    # ------------------------------------------------------------------

    def configs(self) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        rows = [s.to_dict() for s in self.store.list_services()]
        return 200, {"ts_ns": now_ns(), "count": len(rows), "services": rows}

    def get_config(self, service: str, env: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        try:
            data = self.store.get(service, env=env)
        except CFGError as exc:
            return exc.http_status, error_envelope(exc)
        return 200, {"ts_ns": now_ns(), "config": data}

    def put_config(self, service: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        if not isinstance(body, dict):
            from .errors import InvalidPayloadError
            return 400, error_envelope(InvalidPayloadError(service))

        env = body.get("env")
        updated_by = str(body.get("updated_by", "") or "")
        note = str(body.get("note", "") or "")
        if isinstance(body.get("payload"), dict):
            payload: Dict[str, Any] = body["payload"]
        else:
            payload = {k: v for k, v in body.items() if k not in ("env", "updated_by", "note")}

        try:
            blob, created = self.store.upsert(service, payload, env=env,
                                              updated_by=updated_by, note=note)
        except CFGError as exc:
            return exc.http_status, error_envelope(exc)

        # best-effort fan-out (never blocks / fails the mutation)
        self.audit.record(action="config_upsert", actor=updated_by, detail={
            "service": service, "revision": blob.revision, "created": created,
            "hash": blob.hash, "note": note,
        })
        if created:
            self.alerting.fire(
                category="config.new_service", severity="WARNING",
                message=f"new service configuration registered: {service}",
                context={"service": service, "revision": blob.revision},
            )

        return 200, {"ts_ns": now_ns(), "created": created, "config": blob.to_dict()}

    def reload(self) -> Tuple[int, Dict[str, Any]]:
        """Re-broadcast the current version table. A no-op refresh for consumers."""
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        table = self.store.versions()
        return 200, {"ts_ns": now_ns(), "reloaded": True, "versions": table}

    def versions(self, service: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        try:
            data = self.store.versions(service)
        except CFGError as exc:
            return exc.http_status, error_envelope(exc)
        return 200, {"ts_ns": now_ns(), **data}

    # ------------------------------------------------------------------
    # Change log + long-poll
    # ------------------------------------------------------------------

    def changes(self, since: int = 0, limit: int = 100) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        events = self.store.changes(since=since, limit=limit)
        return 200, {"ts_ns": now_ns(), "count": len(events), "events": events}

    def watch(self, since: int = 0, timeout_ms: Optional[int] = None,
              service: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        result = self.store.wait_for_change(since=since, timeout_ms=timeout_ms, service=service)
        return 200, {"ts_ns": now_ns(), **result}

    # ------------------------------------------------------------------
    # Environment overrides
    # ------------------------------------------------------------------

    def set_override(self, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        if not isinstance(body, dict):
            from .errors import InvalidPayloadError
            return 400, error_envelope(InvalidPayloadError("<override>"))
        service = str(body.get("service", "") or "")
        env = str(body.get("env", CONFIG.overrides.default_env) or CONFIG.overrides.default_env)
        override = body.get("override")
        if not service:
            from .errors import InvalidFlagError
            return 400, error_envelope(InvalidFlagError("missing 'service'", context={}))
        if not isinstance(override, dict):
            from .errors import InvalidPayloadError
            return 400, error_envelope(InvalidPayloadError(service))

        updated_by = str(body.get("updated_by", "") or "")
        note = str(body.get("note", "") or "")
        try:
            result = self.store.set_override(service, env, override,
                                             updated_by=updated_by, note=note)
        except CFGError as exc:
            return exc.http_status, error_envelope(exc)

        self.audit.record(action="override_set", actor=updated_by, detail={
            "service": service, "env": env, "keys": sorted(override.keys()), "note": note,
        })
        self.alerting.fire(
            category="config.override", severity="WARNING",
            message=f"environment override applied for {service} (env={env})",
            context={"service": service, "env": env},
        )
        return 200, {"ts_ns": now_ns(), **result}

    def clear_override(self, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        body = body or {}
        service = str(body.get("service", "") or "")
        env = body.get("env")
        updated_by = str(body.get("updated_by", "") or "")
        note = str(body.get("note", "") or "")
        if not service:
            from .errors import InvalidFlagError
            return 400, error_envelope(InvalidFlagError("missing 'service'", context={}))
        result = self.store.clear_override(service, env, updated_by=updated_by, note=note)
        if result.get("found"):
            self.audit.record(action="override_clear", actor=updated_by, detail=result)
        return 200, {"ts_ns": now_ns(), **result}

    def list_overrides(self, service: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        data = self.store.list_overrides(service)
        return 200, {"ts_ns": now_ns(), **data}

    # ------------------------------------------------------------------
    # Feature flags
    # ------------------------------------------------------------------

    def flags(self) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        rows = [f.to_dict() for f in self.store.list_flags()]
        return 200, {"ts_ns": now_ns(), "count": len(rows), "flags": rows}

    def set_flag(self, name: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        body = body or {}
        if "value" not in body:
            from .errors import InvalidFlagError
            return 400, error_envelope(InvalidFlagError("missing 'value'", context={"name": name}))
        value = body["value"]
        description = body.get("description")
        updated_by = str(body.get("updated_by", "") or "")
        note = str(body.get("note", "") or "")
        try:
            flag = self.store.set_flag(name, value, description=description,
                                       updated_by=updated_by, note=note)
        except CFGError as exc:
            return exc.http_status, error_envelope(exc)

        self.audit.record(action="flag_change", actor=updated_by, detail={
            "name": name, "value": value, "note": note,
        })
        self.alerting.fire(
            category="config.flag", severity="WARNING",
            message=f"feature flag {name} set to {value!r}",
            context={"name": name, "value": value},
        )
        return 200, {"ts_ns": now_ns(), "flag": flag.to_dict()}

    def stats(self) -> Tuple[int, Dict[str, Any]]:
        if self.store is None:
            return 503, error_envelope(CFGError("config store not initialized"))
        return 200, {"store": self.store.stats_view()}
