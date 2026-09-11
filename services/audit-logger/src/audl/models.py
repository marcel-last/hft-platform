"""audit_logger — domain models, canonical hashing, and clocks.

The audit trail is a **hash chain** of immutable :class:`EventRecord` rows.  Each
record carries:

* ``event_id``  — globally unique, assigned by the service: ``EVT-`` + 12 hex
  digits drawn from a monotonic logical sequence (the counter survives dedup
  folds, so ids are strictly increasing along the chain).
* ``ts_ns``     — receive-time nanosecond epoch stamp (CONVENTIONS §2).
* ``source``    — emitting service (e.g. ``execution-gateway``).
* ``kind``      — what happened (e.g. ``order.submitted``).
* ``actor``     — who/what caused it (e.g. a user id or a component name).
* ``data``      — free-form JSON object with the event payload.
* ``prev_hash`` — ``content_hash`` of the previous record (or
  :data:`EMPTY_HASH` for the chain head).
* ``hash``      — this record's own :func:`content_hash`.

:func:`content_hash` is a SHA-256 (stdlib ``hashlib``) over the record's
**canonical JSON encoding**: keys sorted at every level, no whitespace,
ASCII-safe.  Canonicalization makes the hash reproducible from the wire
representation alone, which is what makes the chain self-verifying: anyone who
recomputes every ``hash`` and every ``prev_hash`` link can prove no record was
added, removed, reordered, or edited.

Clocks follow the platform pattern (CONVENTIONS §10): production uses
:class:`SystemClock`; tests inject :class:`ManualClock` so time-dependent
behaviour (dedup expiry, stats windows) is deterministic with no sleeping.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

SERVICE_NAME = "audit-logger"
SERVICE_VERSION = "1.0.0"

#: 32 zero bytes as a hex string — the genesis ``prev_hash`` of every chain.
EMPTY_HASH = "0" * 64

#: SHA-256 of ``EMPTY_HASH`` — the hash assigned to the (synthetic) chain
#: head when the chain is empty, so ``/verify-chain`` has a stable baseline.
HEAD_HASH = hashlib.sha256(EMPTY_HASH.encode("ascii")).hexdigest()

#: Prefix and hex-digit count for event ids (``EVT-`` + 12 lowercase hex).
EVENT_ID_PREFIX = "EVT-"
EVENT_ID_HEX_LEN = 12

#: Maximum depth of free-form event payloads (kept small on purpose: audit
#: events are facts, not documents — deep nesting is a smell).
MAX_DATA_DEPTH = 8

#: Maximum characters a single string inside ``data`` may hold.
MAX_STRING_LEN = 8192


# ---------------------------------------------------------------------------
# Canonical JSON + content hashing
# ---------------------------------------------------------------------------

def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` to deterministic, byte-stable JSON.

    * object keys are sorted at **every** nesting level (``sort_keys=True``),
    * there is no insignificant whitespace (compact separators),
    * non-ASCII is escaped (``ensure_ascii=True``) so the byte stream depends
      only on the logical value, never on a locale or encoder default.

    This is the exact byte string that :func:`content_hash` hashes, so the hash
    is reproducible from any JSON wire representation of the same value.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    """Return the lowercase hex SHA-256 of the canonical encoding of ``value``."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _validate_data(value: Any, path: str, depth: int) -> Optional[str]:
    """Return an error string if ``value`` is not a valid ``data`` payload.

    Enforced invariants: depth ≤ :data:`MAX_DATA_DEPTH`, only JSON-native
    scalars/containers, no NaN/Infinity (they break round-trip equality), and
    string length ≤ :data:`MAX_STRING_LEN`.  A single error is returned — the
    first violation found — so the caller can surface it in one envelope.
    """
    if depth > MAX_DATA_DEPTH:
        return f"field {path}: nesting exceeds {MAX_DATA_DEPTH} levels"
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                return f"field {path}: object keys must be strings"
            err = _validate_data(child, f"{path}.{key}", depth + 1)
            if err:
                return err
        return None
    if isinstance(value, list):
        for idx, child in enumerate(value):
            err = _validate_data(child, f"{path}[{idx}]", depth + 1)
            if err:
                return err
        return None
    if isinstance(value, str):
        if len(value) > MAX_STRING_LEN:
            return (
                f"field {path}: string length {len(value)} exceeds "
                f"{MAX_STRING_LEN}"
            )
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return f"field {path}: NaN/Infinity are not allowed in audit data"
        return None
    if _is_scalar(value):
        return None
    return f"field {path}: unsupported JSON type {type(value).__name__}"


# ---------------------------------------------------------------------------
# Clocks (deterministic time in tests, CONVENTIONS §10)
# ---------------------------------------------------------------------------

class Clock:
    """Time source interface: nanosecond epoch stamps."""

    def now_ns(self) -> int:
        raise NotImplementedError

    def set(self, ts_ns: int) -> None:  # pragma: no cover - manual only
        raise TypeError("this clock is not settable")


class SystemClock(Clock):
    """Wall-clock time from :mod:`time` (production)."""

    def now_ns(self) -> int:
        return time.time_ns()


class ManualClock(Clock):
    """A fixed, test-controlled clock.

    ``advance()`` moves the clock forward by a nanosecond delta; ``set()``
    jumps to an absolute stamp.  No sleeping is ever needed to test
    time-dependent behaviour.
    """

    def __init__(self, start_ns: int = 0) -> None:
        self._ns = int(start_ns)

    def now_ns(self) -> int:
        return self._ns

    def set(self, ts_ns: int) -> None:
        self._ns = int(ts_ns)

    def advance(self, ns: int) -> None:
        self._ns += int(ns)


# ---------------------------------------------------------------------------
# Event id helpers
# ---------------------------------------------------------------------------

def format_event_id(seq: int) -> str:
    """Render logical sequence ``seq`` as a canonical event id."""
    return f"{EVENT_ID_PREFIX}{seq:0{EVENT_ID_HEX_LEN}x}"


def parse_event_id(raw: str) -> Optional[int]:
    """Parse ``EVT-<12 hex>`` back to its logical sequence.

    Returns ``None`` for anything malformed (bad prefix, wrong length,
    non-hex digits, uppercase) so callers can answer ``AUD-202`` precisely.
    """
    if not isinstance(raw, str) or not raw.startswith(EVENT_ID_PREFIX):
        return None
    body = raw[len(EVENT_ID_PREFIX):]
    if len(body) != EVENT_ID_HEX_LEN:
        return None
    try:
        seq = int(body, 16)
    except ValueError:
        return None
    # Reject values that would render with leading zeros (e.g. "EVT-00...01a"
    # is well-formed, but "EVT-00000000001A" must fail the lowercase check).
    if format_event_id(seq) != raw:
        return None
    return seq


# ---------------------------------------------------------------------------
# Event record (immutable chain node)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EventRecord:
    """One immutable node of the audit hash chain.

    Fields mirror the on-wire names exactly, so ``to_dict()`` round-trips with
    the API and the hash is always verifiable from the wire representation.
    """

    event_id: str
    ts_ns: int
    source: str
    kind: str
    actor: str
    data: Dict[str, Any] = field(default_factory=dict)
    prev_hash: str = EMPTY_HASH
    hash: str = ""

    # -- hashing -----------------------------------------------------------

    def hash_input(self) -> Dict[str, Any]:
        """The exact dict whose canonical hash is :attr:`hash`.

        ``hash`` itself is excluded (self-reference is impossible) and so is
        ``data``-free ordering: canonical_json sorts keys, so field order here
        is irrelevant to the digest.
        """
        return {
            "event_id": self.event_id,
            "ts_ns": self.ts_ns,
            "source": self.source,
            "kind": self.kind,
            "actor": self.actor,
            "data": self.data,
            "prev_hash": self.prev_hash,
        }

    def recompute_hash(self) -> str:
        """Re-derive the content hash from the record's own fields."""
        return content_hash(self.hash_input())

    # -- wire --------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "ts_ns": self.ts_ns,
            "source": self.source,
            "kind": self.kind,
            "actor": self.actor,
            "data": self.data,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    def to_json_line(self) -> str:
        """Compact single-line JSON for the /export stream (no trailing \\n)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def is_valid_id(raw: str) -> bool:
        return parse_event_id(raw) is not None


# ---------------------------------------------------------------------------
# Request-body validation (POST /events)
# ---------------------------------------------------------------------------

def validate_event_body(body: Any) -> Optional[tuple]:
    """Validate a raw ``POST /events`` body.

    Returns ``None`` when the body is acceptable, otherwise a
    ``(field, detail)`` pair describing the **first** violation found, so the
    controller can map it to a precise ``AUD-201`` envelope.

    Accepted shape::

        {
          "source": "execution-gateway",   # required, non-empty, <= 128 chars
          "kind":   "order.submitted",      # required, non-empty, <= 128 chars
          "actor":  "strat-momentum-01",    # required, non-empty, <= 128 chars
          "data":   {...}                   # optional JSON object (validated)
        }

    ``ts_ns`` is deliberately **rejected** if present: the server stamps its
    own receive time (via the injected :class:`Clock`) so the audit trail
    records *when the logger saw the fact*, which is what a tamper-evidence
    guarantee is about.  Submitters' own wire timestamps belong inside
    ``data`` (e.g. ``data.rt``), where they are hashed along with everything
    else.
    """
    if not isinstance(body, dict):
        return ("", f"body must be a JSON object, got {type(body).__name__}")

    for field_name in ("source", "kind", "actor"):
        value = body.get(field_name)
        if not isinstance(value, str):
            return (field_name, f"{field_name} is required and must be a string")
        if not value.strip():
            return (field_name, f"{field_name} must be non-empty")
        if len(value) > 128:
            return (field_name, f"{field_name} length {len(value)} exceeds 128")

    if "ts_ns" in body:
        return ("ts_ns", "ts_ns is not accepted; the server stamps receive time")

    if "data" in body:
        data = body["data"]
        if not isinstance(data, dict):
            return ("data", "data must be a JSON object when present")
        err = _validate_data(data, "data", 0)
        if err:
            return ("data", err)
    return None
