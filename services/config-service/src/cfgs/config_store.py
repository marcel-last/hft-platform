"""config_service — configuration store core.

The :class:`ConfigStore` is the heart of S11.  It is a thread-safe, in-memory
store that owns:

* **versioned per-service blobs** — each service has exactly one live blob plus
  a bounded revision history; every ``upsert`` bumps the revision and records a
  change event;
* **environment overrides** — partial dicts applied on top of the live blob at
  read time (deep-merged by default);
* **feature flags** — platform-wide typed flags with bounded per-flag history;
* **a change log + long-poll** — every mutation appends a monotonically
  sequenced :class:`~cfgs.models.ChangeEvent`; consumers hold an open
  ``wait_for_change(since, timeout_ms)`` call and are woken the instant a new
  event with ``seq > since`` lands (or after the timeout elapses).

The store never blocks on I/O; all fan-out to S10/S13 happens in the controller
layer so the mutation path stays fast and deterministic.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG, ServiceConfig
from .errors import (
    InvalidFlagError,
    InvalidPayloadError,
    StoreFullError,
    UnknownServiceError,
)
from .models import (
    ChangeEvent,
    ConfigBlob,
    FlagState,
    ServiceInfo,
    content_hash,
    deep_merge,
    now_ns,
)

logger = logging.getLogger("cfgs.config_store")


class ConfigStore:
    """Thread-safe centralized configuration store."""

    def __init__(self, cfg: Optional[ServiceConfig] = None) -> None:
        self.cfg = cfg or CONFIG
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)

        # service name -> live blob
        self._blobs: Dict[str, ConfigBlob] = {}
        # service name -> bounded list of prior blobs (oldest first)
        self._history: Dict[str, List[ConfigBlob]] = {}
        # service name -> env -> override dict
        self._overrides: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # flag name -> FlagState
        self._flags: Dict[str, FlagState] = {}

        # change log (bounded) + sequence counters
        self._changes: List[ChangeEvent] = []
        self._next_seq: int = 1
        self._seq_by_service: Dict[str, int] = {}

        # stats counters
        self.upserts_total: int = 0
        self.overrides_set_total: int = 0
        self.overrides_cleared_total: int = 0
        self.flag_changes_total: int = 0
        self.reads_total: int = 0
        self.long_poll_wakes: int = 0
        self.long_poll_timeouts: int = 0

    # ------------------------------------------------------------------
    # Change-log bookkeeping (call with the lock held)
    # ------------------------------------------------------------------

    def _record_change(self, kind: str, service: str, *, revision: int = 0,
                       actor: str = "", note: str = "") -> ChangeEvent:
        seq = self._next_seq
        self._next_seq += 1
        event = ChangeEvent(
            seq=seq, kind=kind, service=service,
            revision=revision, actor=actor, note=note, ts_ns=now_ns(),
        )
        self._changes.append(event)
        limit = self.cfg.store.max_change_events
        if len(self._changes) > limit:
            del self._changes[: len(self._changes) - limit]
        self._seq_by_service[service] = seq
        return event

    def _latest_seq(self, service: Optional[str] = None) -> int:
        """Highest sequence number overall (or for one service). Call with lock held."""
        if service is not None:
            return self._seq_by_service.get(service, 0)
        return self._changes[-1].seq if self._changes else 0

    # ------------------------------------------------------------------
    # Config CRUD
    # ------------------------------------------------------------------

    def upsert(self, service: str, payload: Any, *, env: Optional[str] = None,
               updated_by: str = "", note: str = "") -> Tuple[ConfigBlob, bool]:
        """Create or update a service's config blob. Returns ``(blob, created)``."""
        if not isinstance(payload, dict):
            raise InvalidPayloadError(service)

        with self._cv:
            existing = self._blobs.get(service)
            if existing is None and len(self._blobs) >= self.cfg.store.max_services:
                raise StoreFullError(self.cfg.store.max_services)

            revision = (existing.revision + 1) if existing else 1
            blob = ConfigBlob(
                service=service,
                payload=dict(payload),
                revision=revision,
                env=env or (existing.env if existing else self.cfg.overrides.default_env),
                updated_by=updated_by,
                note=note,
            )

            if existing is not None:
                # keep the bounded prior-revision history (oldest first)
                hist = self._history.setdefault(service, [])
                hist.append(existing)
                cap = self.cfg.store.max_revisions_per_service
                if len(hist) > cap:
                    del hist[: len(hist) - cap]

            created = existing is None
            self._blobs[service] = blob
            self.upserts_total += 1
            self._record_change(
                "config_update", service, revision=revision, actor=updated_by, note=note
            )
            logger.info("config upsert service=%s rev=%d created=%s by=%s",
                        service, revision, created, updated_by or "-")
            self._cv.notify_all()
            return blob, created

    def get(self, service: str, *, env: Optional[str] = None) -> Dict[str, Any]:
        """Return the effective config for ``service`` (blob + env override merged)."""
        with self._cv:
            blob = self._blobs.get(service)
            if blob is None:
                raise UnknownServiceError(service)
            self.reads_total += 1
            eff_env = env or blob.env
            merged = deep_merge(blob.payload, self._overrides.get(service, {}).get(eff_env, {}))
            return {
                "service": service,
                "payload": merged,
                "revision": blob.revision,
                "hash": content_hash(merged),
                "base_hash": blob.hash,
                "env": eff_env,
                "override_applied": eff_env in self._overrides.get(service, {}),
                "updated_ns": blob.updated_ns,
                "updated_by": blob.updated_by,
            }

    def get_raw(self, service: str) -> Dict[str, Any]:
        """Return the raw (unmerged) stored blob for ``service``."""
        with self._cv:
            blob = self._blobs.get(service)
            if blob is None:
                raise UnknownServiceError(service)
            return blob.to_dict()

    def list_services(self) -> List[ServiceInfo]:
        """Summary rows for every tracked service, sorted by name."""
        with self._cv:
            infos = []
            for name in sorted(self._blobs):
                blob = self._blobs[name]
                infos.append(ServiceInfo(
                    service=name,
                    revision=blob.revision,
                    hash=blob.hash,
                    env=blob.env,
                    updated_ns=blob.updated_ns,
                    updated_by=blob.updated_by,
                    has_overrides=bool(self._overrides.get(name)),
                ))
            return infos

    def versions(self, service: Optional[str] = None) -> Dict[str, Any]:
        """Version table. With a service name, includes its revision history."""
        with self._cv:
            if service is not None:
                blob = self._blobs.get(service)
                if blob is None:
                    raise UnknownServiceError(service)
                return {
                    "service": service,
                    "current_revision": blob.revision,
                    "current_hash": blob.hash,
                    "history": [b.to_dict() for b in self._history.get(service, [])],
                }
            rows = [
                {"service": name, "revision": b.revision, "hash": b.hash}
                for name, b in sorted(self._blobs.items())
            ]
            return {
                "count": len(rows),
                "latest_seq": self._latest_seq(),
                "services": rows,
            }

    # ------------------------------------------------------------------
    # Environment overrides
    # ------------------------------------------------------------------

    def set_override(self, service: str, env: str, override: Any, *,
                     updated_by: str = "", note: str = "") -> Dict[str, Any]:
        """Install (or replace) an environment override for ``service``/``env``."""
        if not isinstance(override, dict):
            raise InvalidPayloadError(service)
        with self._cv:
            envs = self._overrides.setdefault(service, {})
            envs[env] = dict(override)
            self.overrides_set_total += 1
            blob = self._blobs.get(service)
            self._record_change(
                "override_set", service,
                revision=blob.revision if blob else 0, actor=updated_by, note=note,
            )
            logger.info("override set service=%s env=%s by=%s", service, env, updated_by or "-")
            self._cv.notify_all()
            return {"service": service, "env": env, "found": True, "override": dict(override)}

    def clear_override(self, service: str, env: Optional[str] = None, *,
                       updated_by: str = "", note: str = "") -> Dict[str, Any]:
        """Remove an override (one env, or all envs when ``env`` is omitted)."""
        with self._cv:
            envs = self._overrides.get(service)
            if not envs:
                return {"service": service, "cleared": [], "found": False}
            targets = [env] if env else list(envs.keys())
            cleared = []
            for t in targets:
                if t in envs:
                    del envs[t]
                    cleared.append(t)
            if not cleared:
                return {"service": service, "cleared": [], "found": False}
            if not envs:
                self._overrides.pop(service, None)
            self.overrides_cleared_total += 1
            blob = self._blobs.get(service)
            self._record_change(
                "override_clear", service,
                revision=blob.revision if blob else 0, actor=updated_by, note=note,
            )
            logger.info("override clear service=%s envs=%s by=%s", service, cleared, updated_by or "-")
            self._cv.notify_all()
            return {"service": service, "cleared": cleared, "found": True}

    def list_overrides(self, service: Optional[str] = None) -> Dict[str, Any]:
        """Return all overrides, optionally restricted to one service."""
        with self._cv:
            if service is not None:
                return {"service": service, "overrides": dict(self._overrides.get(service, {}))}
            rows = [
                {"service": s, "envs": {e: dict(v) for e, v in envs.items()}}
                for s, envs in sorted(self._overrides.items())
            ]
            return {"count": len(rows), "overrides": rows}

    # ------------------------------------------------------------------
    # Feature flags
    # ------------------------------------------------------------------

    def set_flag(self, name: str, value: Any, *, description: Optional[str] = None,
                 updated_by: str = "", note: str = "") -> FlagState:
        """Create or update a feature flag; records a bounded history entry."""
        with self._cv:
            if len(self._flags) >= self.cfg.flags.max_flags and name not in self._flags:
                raise InvalidFlagError(
                    f"flag limit reached ({self.cfg.flags.max_flags})",
                    context={"name": name},
                )
            flag = self._flags.get(name)
            if flag is None:
                flag = FlagState(name=name, value=value,
                                 description=description or "", updated_by=updated_by)
                self._flags[name] = flag
            else:
                flag.value = value
                if description is not None:
                    flag.description = description
                flag.updated_by = updated_by or flag.updated_by
            flag.updated_ns = now_ns()
            entry = {"value": flag.value, "updated_ns": flag.updated_ns,
                     "updated_by": flag.updated_by, "note": note}
            flag.history.append(entry)
            cap = self.cfg.flags.max_flag_history
            if len(flag.history) > cap:
                del flag.history[: len(flag.history) - cap]

            self.flag_changes_total += 1
            self._record_change(
                "flag_change", name, actor=updated_by, note=note
            )
            logger.info("flag set %s=%r by=%s", name, value, updated_by or "-")
            self._cv.notify_all()
            return flag

    def get_flag(self, name: str) -> FlagState:
        with self._cv:
            flag = self._flags.get(name)
            if flag is None:
                raise InvalidFlagError(f"unknown flag {name!r}", context={"name": name})
            return flag

    def list_flags(self) -> List[FlagState]:
        with self._cv:
            return [self._flags[n] for n in sorted(self._flags)]

    # ------------------------------------------------------------------
    # Change log + long-poll
    # ------------------------------------------------------------------

    def changes(self, since: int = 0, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Return change events with ``seq > since`` (ascending), bounded by ``limit``."""
        with self._cv:
            rows = [c for c in self._changes if c.seq > since]
            if limit and limit > 0:
                rows = rows[-limit:]
            return [r.to_dict() for r in rows]

    def wait_for_change(self, since: int = 0, timeout_ms: Optional[int] = None,
                        service: Optional[str] = None) -> Dict[str, Any]:
        """Long-poll: block until a new event (``seq > since``, optionally scoped to
        ``service``) arrives or the timeout elapses.

        Returns ``{"changed": bool, "latest_seq": int, "events": [...]}``.
        """
        if timeout_ms is None:
            timeout_ms = self.cfg.poll.default_timeout_ms
        timeout_ms = max(1, min(int(timeout_ms), self.cfg.poll.max_timeout_ms))
        granularity_s = self.cfg.poll.poll_granularity_ms / 1000.0

        def _predicate() -> bool:
            # True only when a *new* event (seq > since) has landed — scoped to
            # ``service`` when given. Using the per-service high-water mark would
            # be True for any service that merely has an older config, which is
            # not what a long-poll consumer wants.
            if service is not None:
                return any(c.seq > since and c.service == service for c in self._changes)
            return self._latest_seq() > since

        deadline_ns = now_ns() + timeout_ms * 1_000_000
        with self._cv:
            if _predicate():
                changed = True
            else:
                # wait in granularity-sized slices so we re-check the deadline and
                # can be interrupted promptly by a new event
                while not _predicate():
                    remaining_ns = deadline_ns - now_ns()
                    if remaining_ns <= 0:
                        break
                    slice_s = min(granularity_s, remaining_ns / 1_000_000_000)
                    self._cv.wait(timeout=max(0.001, slice_s))
                changed = _predicate()

            if changed:
                self.long_poll_wakes += 1
            else:
                self.long_poll_timeouts += 1

            events = [c.to_dict() for c in self._changes
                      if c.seq > since and (service is None or c.service == service)]
            return {
                "changed": changed,
                "since": since,
                "latest_seq": self._seq_by_service.get(service, 0) if service else self._latest_seq(),
                "events": events,
            }

    # ------------------------------------------------------------------
    # Read views
    # ------------------------------------------------------------------

    def stats_view(self) -> Dict[str, Any]:
        with self._cv:
            return {
                "services_tracked": len(self._blobs),
                "overrides_active": sum(len(v) for v in self._overrides.values()),
                "flags": len(self._flags),
                "change_events": len(self._changes),
                "latest_seq": self._latest_seq(),
                "upserts_total": self.upserts_total,
                "overrides_set_total": self.overrides_set_total,
                "overrides_cleared_total": self.overrides_cleared_total,
                "flag_changes_total": self.flag_changes_total,
                "reads_total": self.reads_total,
                "long_poll_wakes": self.long_poll_wakes,
                "long_poll_timeouts": self.long_poll_timeouts,
            }

    def readiness_reasons(self) -> List[str]:
        """Flat list of reasons the store is not ready (empty = ready)."""
        with self._cv:
            reasons: List[str] = []
            if len(self._blobs) == 0:
                reasons.append("no service configurations stored yet")
            return reasons
