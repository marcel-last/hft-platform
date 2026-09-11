"""config_service — core domain models.

Models in this module describe the configuration state maintained by S11:

* :class:`ConfigBlob`  — one versioned configuration document for a service,
  carrying its canonical content hash, revision number and provenance;
* :class:`ChangeEvent` — one entry in the platform-wide change log (a config
  update, an override change or a flag flip);
* :class:`ServiceInfo` — the summary row returned by ``GET /configs``;
* :class:`FlagState`   — one feature flag with its typed value + history.

plus the ``now_ns()`` hot-path timestamp helper and two pure helpers:
:class:`~cfgs.models.canonical_json` (stable serialization used to derive a
content hash) and :func:`deep_merge` (recursive dict merge used for overrides).

All timestamps are int64 nanoseconds since the Unix epoch.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def now_ns() -> int:
    """Current time as int64 nanoseconds since the Unix epoch (hot-path convention)."""
    return time.time_ns()


# ---------------------------------------------------------------------------
# Pure helpers (module-level so they are trivially unit-testable)
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """Stable JSON serialization for hashing / equality.

    Object keys are sorted and whitespace is removed so that two logically
    equal documents always produce the same byte string (and therefore the
    same content hash), regardless of insertion order.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` onto a copy of ``base`` and return the result.

    * dict + dict   -> merged recursively
    * anything else -> override value wins wholesale (including lists)
    """
    if not isinstance(base, dict):
        base = {}
    if not isinstance(override, dict):
        return dict(base)
    out: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def content_hash(payload: Dict[str, Any]) -> str:
    """SHA-256 hex digest of the canonical JSON form of ``payload``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Configuration blobs
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ConfigBlob:
    """One versioned configuration document for a single service.

    ``payload`` is the raw (unmerged) configuration tree as submitted via
    ``PUT /config/{service}``.  Environment overrides are applied at read time,
    never stored on the blob itself.
    """

    service: str
    payload: Dict[str, Any]
    revision: int = 1
    hash: str = ""
    env: str = "production"
    updated_ns: int = 0
    updated_by: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = content_hash(self.payload)
        if not self.updated_ns:
            self.updated_ns = now_ns()

    @classmethod
    def from_wire(cls, service: str, body: Dict[str, Any], *, env: str = "production",
                  revision: int = 1, updated_by: str = "") -> "ConfigBlob":
        """Build a blob from a ``PUT /config/{service}`` request body.

        The body may carry the payload under a ``payload`` key or be the payload
        itself; optional provenance fields (env, updated_by, note) are honoured.
        """
        if isinstance(body.get("payload"), dict):
            payload: Dict[str, Any] = dict(body["payload"])
        elif isinstance(body, dict):
            payload = {k: v for k, v in body.items()
                       if k not in ("env", "updated_by", "note")}
        else:  # pragma: no cover - defensive; callers pass dicts
            payload = {}
        return cls(
            service=service,
            payload=payload,
            revision=revision,
            env=str(body.get("env", env) or env),
            updated_by=str(body.get("updated_by", updated_by) or updated_by),
            note=str(body.get("note", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service,
            "payload": self.payload,
            "revision": self.revision,
            "hash": self.hash,
            "env": self.env,
            "updated_ns": self.updated_ns,
            "updated_by": self.updated_by,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ChangeEvent:
    """One entry in the platform-wide change log.

    ``kind`` is one of ``config_update``, ``override_set``, ``override_clear`` or
    ``flag_change``.  Every event carries a monotonically increasing sequence so
    long-poll consumers can resume from a stable offset (``since``).
    """

    seq: int
    kind: str
    service: str
    ts_ns: int = 0
    revision: int = 0
    actor: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.ts_ns:
            self.ts_ns = now_ns()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "service": self.service,
            "ts_ns": self.ts_ns,
            "revision": self.revision,
            "actor": self.actor,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Per-service summary
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ServiceInfo:
    """Summary row for one service as returned by ``GET /configs``."""

    service: str
    revision: int = 0
    hash: str = ""
    env: str = "production"
    updated_ns: int = 0
    updated_by: str = ""
    has_overrides: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service,
            "revision": self.revision,
            "hash": self.hash,
            "env": self.env,
            "updated_ns": self.updated_ns,
            "updated_by": self.updated_by,
            "has_overrides": self.has_overrides,
        }


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FlagState:
    """One platform-wide feature flag with its typed value and change history."""

    name: str
    value: Any = False
    description: str = ""
    updated_ns: int = 0
    updated_by: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self, include_history: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "description": self.description,
            "updated_ns": self.updated_ns,
            "updated_by": self.updated_by,
        }
        if include_history:
            out["history"] = list(self.history)
        return out
