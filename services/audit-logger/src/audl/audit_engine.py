"""audit_logger — the hash-chained audit store.

:class:`AuditEngine` owns the entire tamper-evidence guarantee:

* **Append-only chain.** Every accepted event becomes an immutable
  :class:`EventRecord` whose ``prev_hash`` is the ``hash`` of the current tail
  (or :data:`EMPTY_HASH` for the first record) and whose ``hash`` is the
  SHA-256 of its own canonical content.  Nothing in the engine ever mutates or
  deletes a record — the only exception is bounded *eviction* of the oldest
  records when ``chain.max_events`` is exceeded (see
  :meth:`AuditEngine._rechain_after_eviction`).
* **Self-verification.** :meth:`AuditEngine.verify_chain` walks the whole
  chain and recomputes every hash and every link, reporting the first bad
  index (0-based, chain order) when something fails.  :meth:`AuditEngine.verify`
  checks a single record the same way and is used for idempotent appends.
* **Idempotent append.** A repeat of ``(source, kind, dedup_key)`` inside the
  dedup window returns the *original* record (``status="duplicate"``) instead
  of appending a second node, so at-least-once delivery from S4/S5/S11 never
  double-counts an action.
* **Deterministic time.** All timestamps come from the injected
  :class:`~audl.models.Clock`, so tests control time without sleeping.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .config import AuditConfig
from .errors import InvalidEventError
from .models import (
    EMPTY_HASH,
    Clock,
    EventRecord,
    SystemClock,
    content_hash,
    format_event_id,
    validate_event_body,
)

logger = logging.getLogger("audl.audit_engine")


class AppendOutcome:
    """Result of :meth:`AuditEngine.append`.

    ``status`` is ``"appended"`` (a new chain node was created) or
    ``"duplicate"`` (an earlier record for the same dedup key inside the
    window was returned instead).  ``record`` is the stored record either way.
    """

    __slots__ = ("status", "record", "reason")

    def __init__(self, status: str, record: EventRecord, reason: str = "") -> None:
        self.status = status
        self.record = record
        self.reason = reason

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.status, "record": self.record.to_dict()}
        if self.reason:
            out["reason"] = self.reason
        return out


class VerifyReport:
    """Result of :meth:`AuditEngine.verify_chain` / :meth:`AuditEngine.verify`.

    ``first_bad_index`` is 0-based in chain order and points at the first
    record whose own hash or prev-hash link failed to recompute (``None`` when
    the walk was clean or truncated before finding a fault).  ``first_bad_id``
    is that record's ``event_id`` (``None`` for an empty chain or a clean walk).
    """

    __slots__ = (
        "intact",
        "verified_count",
        "chain_length",
        "first_bad_index",
        "first_bad_id",
        "reason",
        "truncated",
        "head_id",
        "tail_hash",
    )

    def __init__(
        self,
        intact: bool,
        verified_count: int,
        chain_length: int,
        first_bad_index: Optional[int],
        first_bad_id: Optional[str],
        reason: str,
        truncated: bool,
        head_id: Optional[str],
        tail_hash: str,
    ) -> None:
        self.intact = intact
        self.verified_count = verified_count
        self.chain_length = chain_length
        self.first_bad_index = first_bad_index
        self.first_bad_id = first_bad_id
        self.reason = reason
        self.truncated = truncated
        self.head_id = head_id
        self.tail_hash = tail_hash

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "intact": self.intact,
            "verified_count": self.verified_count,
            "chain_length": self.chain_length,
            "truncated": self.truncated,
            "head_id": self.head_id,
            "tail_hash": self.tail_hash,
        }
        if self.first_bad_index is not None:
            out["first_bad_index"] = self.first_bad_index
        if self.first_bad_id is not None:
            out["first_bad_id"] = self.first_bad_id
        if self.reason:
            out["reason"] = self.reason
        return out


class AuditEngine:
    """Thread-safe, in-memory, hash-chained audit log."""

    def __init__(self, cfg: AuditConfig, clock: Optional[Clock] = None) -> None:
        self._cfg = cfg
        self._clock = clock if clock is not None else SystemClock()
        self._lock = threading.RLock()

        # Chain storage: insertion-ordered id -> record.
        self._records: "OrderedDict[str, EventRecord]" = OrderedDict()
        self._last_hash: str = EMPTY_HASH
        self._seq: int = 0

        # Dedup: (source, kind, dedup_key) -> (event_id, stored_ts_ns).
        self._dedup: "OrderedDict[Tuple[str, str, str], Tuple[str, int]]" = OrderedDict()

        # Stats counters.
        self._appended = 0
        self._duplicates = 0
        self._evicted = 0
        self._rejected = 0
        self._by_source: Dict[str, int] = {}
        self._by_kind: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, body: Any, dedup_key: Optional[str] = None) -> AppendOutcome:
        """Validate, de-duplicate, and append one event to the chain.

        ``dedup_key`` is an optional caller-supplied idempotency hint; when
        absent, the key is derived from the canonical JSON of ``data`` so that
        two byte-identical payloads fold together while logically distinct
        payloads (even with identical ``source``/``kind``/``actor``) do not.

        Raises :class:`InvalidEventError` (mapped to AUD-201) on malformed
        input; every rejection is counted in stats.
        """
        problem = validate_event_body(body)
        if problem is not None:
            self._rejected += 1
            raise InvalidEventError(problem[0], problem[1])

        source = body["source"]
        kind = body["kind"]
        actor = body["actor"]
        data = body.get("data", {})

        key = (source, kind, dedup_key if dedup_key is not None else content_hash(data))
        now = self._clock.now_ns()

        with self._lock:
            self._prune_dedup(now)

            if self._cfg.dedup.enabled:
                existing = self._dedup.get(key)
                if existing is not None:
                    orig_id, orig_ts = existing
                    record = self._records.get(orig_id)
                    if record is not None and (now - orig_ts) <= self._cfg.dedup.window_ns:
                        self._duplicates += 1
                        return AppendOutcome(
                            "duplicate",
                            record,
                            reason=f"dedup key ({source}, {kind}) already stored as {orig_id}",
                        )

            # Evict *before* linking the new node: the surviving suffix is
            # re-chained first, so the new record is created against the final
            # tail hash and the record we return is always the stored record
            # (never invalidated by a subsequent re-chain).  The chain length
            # never exceeds max_events.
            while len(self._records) >= self._cfg.chain.max_events:
                self._evict_overflow()

            prev_hash = self._last_hash
            event_id = format_event_id(self._seq)
            self._seq += 1
            record = EventRecord(
                event_id=event_id,
                ts_ns=now,
                source=source,
                kind=kind,
                actor=actor,
                data=data,
                prev_hash=prev_hash,
                hash=content_hash(
                    {
                        "event_id": event_id,
                        "ts_ns": now,
                        "source": source,
                        "kind": kind,
                        "actor": actor,
                        "data": data,
                        "prev_hash": prev_hash,
                    }
                ),
            )
            self._records[event_id] = record
            self._last_hash = record.hash

            if self._cfg.dedup.enabled:
                self._dedup[key] = (event_id, now)
                while len(self._dedup) > self._cfg.dedup.max_keys:
                    self._dedup.popitem(last=False)

            self._appended += 1
            self._by_source[source] = self._by_source.get(source, 0) + 1
            self._by_kind[kind] = self._by_kind.get(kind, 0) + 1

            return AppendOutcome("appended", record)

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def _evict_overflow(self) -> None:
        """Drop the oldest record and re-chain the survivors.

        Called *before* the new node is linked (see :meth:`append`), so the
        re-chained tail is the tail the new record will point at; the returned
        append outcome is never stale.
        """
        old_id, _ = self._records.popitem(last=False)
        self._evicted += 1
        self._rechain_after_eviction()
        logger.warning("evicted oldest audit event %s past retention bound", old_id)

    def _rechain_after_eviction(self) -> None:
        """Sever the chain at the new head and re-hash the surviving suffix.

        The oldest surviving record becomes the new chain head: its
        ``prev_hash`` is reset to :data:`EMPTY_HASH` and its ``hash` (and
        therefore every later ``prev_hash``/``hash``) is recomputed in order.
        The chain is therefore *self-consistent* for its retained window, and
        ``stats.evicted`` + ``stats.head_id`` make the severance visible.
        """
        prev_hash = EMPTY_HASH
        rebuilt: Dict[str, EventRecord] = {}
        for record in self._records.values():
            new_record = EventRecord(
                event_id=record.event_id,
                ts_ns=record.ts_ns,
                source=record.source,
                kind=record.kind,
                actor=record.actor,
                data=record.data,
                prev_hash=prev_hash,
                hash="",
            )
            hashed = EventRecord(
                event_id=new_record.event_id,
                ts_ns=new_record.ts_ns,
                source=new_record.source,
                kind=new_record.kind,
                actor=new_record.actor,
                data=new_record.data,
                prev_hash=new_record.prev_hash,
                hash=new_record.recompute_hash(),
            )
            rebuilt[hashed.event_id] = hashed
            prev_hash = hashed.hash
        self._records.clear()
        self._records.update(rebuilt)
        self._last_hash = prev_hash

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, event_id: str) -> Optional[EventRecord]:
        with self._lock:
            return self._records.get(event_id)

    def oldest_id(self) -> Optional[str]:
        """Id of the oldest retained record, or ``None`` for an empty chain."""
        with self._lock:
            if not self._records:
                return None
            return next(iter(self._records))

    def list_events(
        self,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> List[EventRecord]:
        """Return up to ``limit`` records, **newest first**, optionally filtered."""
        with self._lock:
            records = list(self._records.values())
        if source is not None:
            records = [r for r in records if r.source == source]
        if kind is not None:
            records = [r for r in records if r.kind == kind]
        records.reverse()  # newest first
        return records[: max(limit, 0)]

    def export_lines(
        self,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 5000,
    ) -> List[str]:
        """Chain-ordered JSON lines (oldest → newest), filtered, bounded."""
        with self._lock:
            records = list(self._records.values())
        if source is not None:
            records = [r for r in records if r.source == source]
        if kind is not None:
            records = [r for r in records if r.kind == kind]
        return [r.to_json_line() for r in records[: max(limit, 0)]]

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, record: EventRecord) -> Tuple[bool, str]:
        """Check one record's own hash and its prev-hash link.

        The link check requires the record's predecessor to still be present
        (a record at the head links to :data:`EMPTY_HASH`).  Returns
        ``(ok, reason)``.
        """
        if record.recompute_hash() != record.hash:
            return False, f"record {record.event_id} content hash mismatch"
        idx = self._index_of(record.event_id)
        if idx is None:
            return False, f"record {record.event_id} is not in the chain"
        if idx > 0:
            expected_prev = self._ordered_records()[idx - 1].hash
            if record.prev_hash != expected_prev:
                return False, (
                    f"record {record.event_id} prev_hash does not match "
                    f"predecessor hash"
                )
        elif record.prev_hash != EMPTY_HASH:
            return False, "head record prev_hash is not the empty-hash constant"
        return True, ""

    def verify_chain(self) -> VerifyReport:
        """Walk the whole chain and recompute every hash and link.

        The walk stops at :attr:`chain.max_verify_depth` when the live chain is
        longer and then reports ``truncated=True``.  A clean full walk sets
        ``intact=True``; any hash or link failure pins ``first_bad_index`` /
        ``first_bad_id`` to the first offending record (0-based chain order).
        """
        with self._lock:
            ordered = list(self._records.values())
            max_depth = self._cfg.chain.max_verify_depth

        verified = 0
        prev_hash = EMPTY_HASH
        first_bad_index: Optional[int] = None
        first_bad_id: Optional[str] = None
        reason = ""
        truncated = False

        for idx, record in enumerate(ordered):
            if idx >= max_depth:
                truncated = True
                break
            verified += 1
            if record.recompute_hash() != record.hash:
                first_bad_index = idx
                first_bad_id = record.event_id
                reason = f"record {record.event_id} content hash mismatch"
                break
            if record.prev_hash != prev_hash:
                first_bad_index = idx
                first_bad_id = record.event_id
                reason = f"record {record.event_id} prev_hash link broken"
                break
            prev_hash = record.hash

        return VerifyReport(
            intact=(first_bad_index is None and not truncated),
            verified_count=verified,
            chain_length=len(ordered),
            first_bad_index=first_bad_index,
            first_bad_id=first_bad_id,
            reason=reason,
            truncated=truncated,
            head_id=ordered[0].event_id if ordered else None,
            tail_hash=prev_hash,
        )

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            head_id = next(iter(self._records), None)
            body = {
                "chain_length": len(self._records),
                "appended": self._appended,
                "duplicates": self._duplicates,
                "evicted": self._evicted,
                "rejected": self._rejected,
                "head_id": head_id,
                "tail_hash": self._last_hash,
                "by_source": dict(sorted(self._by_source.items())),
                "by_kind": dict(sorted(self._by_kind.items())),
            }
        report = self.verify_chain()
        body["last_verified"] = report.to_dict()
        return body

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune_dedup(self, now_ns: int) -> None:
        """Drop dedup entries older than the window (caller holds lock)."""
        if not self._cfg.dedup.enabled or not self._dedup:
            return
        expired = [
            key for key, (_, ts) in self._dedup.items() if (now_ns - ts) > self._cfg.dedup.window_ns
        ]
        for key in expired:
            del self._dedup[key]

    def _index_of(self, event_id: str) -> Optional[int]:
        for idx, record in enumerate(self._records.values()):
            if record.event_id == event_id:
                return idx
        return None

    def _ordered_records(self) -> List[EventRecord]:
        return list(self._records.values())
