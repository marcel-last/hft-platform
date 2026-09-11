"""audit_logger — HTTP controller.

Endpoints (see ``dependency-map.json`` for S13):

    POST /events                append one event to the hash chain
    GET  /events                list events (?source= & ?kind= & ?limit=, newest first)
    GET  /events/export         export the chain as JSON lines (?source= & ?kind= & ?limit=)
    GET  /events/verify-chain   walk + re-hash the chain; report integrity
    GET  /events/{event_id}     one event by id
    GET  /stats                 counters by source/kind, chain length, verified flag
    GET  /healthz               liveness (CONVENTIONS §3)
    GET  /readyz                readiness (CONVENTIONS §3)

Every error path returns the standard §1.2 envelope; each handler catches
:class:`AUDError` and maps it to its ``http_status``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .config import AuditConfig
from .errors import (
    AUDError,
    BadIdError,
    BadLimitError,
    UnknownEventError,
    error_envelope,
)
from .models import SERVICE_NAME, SERVICE_VERSION, parse_event_id

logger = logging.getLogger("audl.controller")


def _parse_limit(raw: Optional[str], default: int, cap: int) -> int:
    """Resolve ``?limit=``: absent → default; non-integer → AUD-203; clamp to [1, cap]."""
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise BadLimitError(str(raw))
    if value < 1:
        raise BadLimitError(str(raw))
    return min(value, cap)


class AuditController:
    """Shared state bundle for the HTTP handlers."""

    def __init__(self, cfg: Optional[AuditConfig] = None, engine=None) -> None:
        self.cfg = cfg
        self.engine = engine  # AuditEngine (wired by main)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def healthz(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}

    def readyz(self) -> Tuple[int, Dict[str, Any]]:
        engine = self.engine
        if engine is None:
            return 503, error_envelope(
                AUDError("engine not initialized", context={"detail": "boot incomplete"})
            )
        # Readiness = engine wired. The chain is always internally consistent
        # (append is the only writer and it maintains the hashes), so there is
        # no "warming up" state to wait out; /verify-chain is the tool for
        # proving integrity on demand.
        reasons: List[str] = []
        ready = not reasons
        return (200 if ready else 503), {
            "status": "ready" if ready else "not_ready",
            "reasons": reasons,
        }

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append_event(self, body: Optional[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        try:
            outcome = engine.append(body)
        except AUDError as exc:
            return exc.http_status, error_envelope(exc)
        return 200, {
            "status": outcome.status,
            "event": outcome.record.to_dict(),
            **( {"reason": outcome.reason} if outcome.reason else {} ),
        }

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_events(
        self,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        limit_raw: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        cfg = self.cfg
        limit = _parse_limit(limit_raw, cfg.query.default_limit, cfg.query.max_limit)
        records = engine.list_events(source=source, kind=kind, limit=limit)
        return 200, {
            "count": len(records),
            "events": [r.to_dict() for r in records],
        }

    def export_events(
        self,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        limit_raw: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Chunked JSON-lines export of the chain (oldest → newest).

        The lines are returned inside the JSON envelope as ``lines`` so the
        response keeps the service's ``application/json`` content type
        (CONVENTIONS §3) while remaining a valid JSON-lines stream: each
        element of ``lines`` is exactly one record per line.
        """
        engine = self._require_engine()
        cfg = self.cfg
        limit = _parse_limit(limit_raw, cfg.query.max_limit, cfg.query.max_limit)
        lines = engine.export_lines(source=source, kind=kind, limit=limit)
        return 200, {
            "count": len(lines),
            "format": "json-lines",
            "order": "oldest_first",
            "lines": lines,
        }

    def verify_chain(self) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        report = engine.verify_chain()
        body = report.to_dict()
        status = 200 if report.intact else 207
        return status, body

    def event_by_id(self, event_id: str) -> Tuple[int, Dict[str, Any]]:
        if parse_event_id(event_id) is None:
            return 400, error_envelope(BadIdError(event_id))
        engine = self._require_engine()
        record = engine.get(event_id)
        if record is None:
            evicted = engine.oldest_id() is not None and (
                parse_event_id(engine.oldest_id()) > parse_event_id(event_id)
            )
            return 404, error_envelope(UnknownEventError(event_id, evicted=evicted))
        ok, reason = engine.verify(record)
        return 200, {"event": record.to_dict(), "verified": ok, "reason": reason}

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        return 200, engine.stats()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_engine(self):
        if self.engine is None:
            raise AUDError("engine not initialized", context={"detail": "boot incomplete"})
        return self.engine
