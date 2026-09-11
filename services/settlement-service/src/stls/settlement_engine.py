"""Part 1 of 2 — settlement engine: runs, idempotent settlement, EOD sealing.

The engine owns every :class:`SettlementRun` (one per settlement date).  A
single RLock guards all mutation and read paths; the lock is always held for
the duration of a public method so HTTP handlers never observe a half-written
run.

Settlement semantics (POST /settle)
-----------------------------------

* **Idempotent fills** — each fill id is bound to the canonical digest of its
  validated payload.  Re-submitting a byte-identical fill is a counted no-op
  (``duplicate_fills_ignored``); re-using the id with *different* content is a
  409 :class:`FillIdConflict` (STL-205) because the bookkeeping is ambiguous.
* **Statement replacement** — statement lines are not cumulative.  Each
  ``POST /settle`` carrying ``statement_lines`` for a venue replaces that
  venue's entire prior statement for the run; venues untouched by the body keep
  their lines.  This matches how venues re-issue their daily confirmation.
* **Finalization** — ``finalize_eod(date)`` seals the run: ``kind`` becomes
  ``eod``, ``finalized`` becomes ``True``, and ``content_hash`` is computed
  over the run's canonical snapshot (fills + statement lines + netting), so a
  finalized report is tamper-evident exactly like S13's chain.  Any further
  mutation of a sealed run is a 409 :class:`RunFinalized` (STL-206).
* **Revisions** — every settle call and finalization appends a bounded
  per-date revision entry (``max_runs_per_date_history``), so an operator can
  see exactly when a day's numbers last moved.

Discrepancies are *recomputed lazily* on every read (list/report) from the
run's current fills and statement lines — they are never stored incrementally,
so a statement replacement can never leave a stale finding behind.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import SettlementConfig
from .models import (
    Clock,
    SettlementRun,
    canonical_json,
    content_hash,
    parse_fill,
    parse_settlement_date,
    parse_statement_line,
)
from .errors import FillIdConflict, InvalidSettleRequest, RunFinalized, UnknownRun
from .netting import compute_cash_summary, compute_net_positions
from .reconcile import matching_report


@dataclass
class RevisionEntry:
    """One bounded audit point of a run's history (what changed, when, by what)."""

    seq: int
    ts_ns: int
    action: str          # settle | finalize
    fills_added: int
    fills_duplicated: int
    statement_venues: List[str]
    run_kind: str


class SettlementEngine:
    """Thread-safe owner of every settlement run and the service's counters."""

    def __init__(self, cfg: SettlementConfig, clock: Clock) -> None:
        self.cfg = cfg
        self.clock = clock
        self._lock = threading.RLock()
        self._runs: Dict[str, SettlementRun] = {}
        self._revisions: Dict[str, List[RevisionEntry]] = {}
        self._rev_seq: Dict[str, int] = {}
        self._settle_calls = 0
        self._duplicate_fills = 0
        self._fill_conflicts = 0
        self._finalized_runs = 0
        self._finalization_conflicts = 0

    # ------------------------------------------------------------------
    # Run access
    # ------------------------------------------------------------------

    def _get_run(self, date: str) -> SettlementRun:
        run = self._runs.get(date)
        if run is None:
            raise UnknownRun(
                f"No settlement run exists for date {date}.",
                context={"date": date, "known_dates": self.list_dates()})
        return run

    def _check_open(self, run: SettlementRun) -> None:
        if run.finalized:
            raise RunFinalized(
                f"The settlement run for {run.date} is finalized (EOD closed).",
                context={"date": run.date, "content_hash": run.content_hash})

    def list_dates(self) -> List[str]:
        return sorted(self._runs)

    def _record_revision(self, date: str, action: str, fills_added: int,
                         fills_dup: int, statement_venues: List[str], run: SettlementRun) -> None:
        revs = self._revisions.setdefault(date, [])
        seq = self._rev_seq.get(date, 0) + 1
        self._rev_seq[date] = seq
        revs.append(RevisionEntry(
            seq=seq, ts_ns=self.clock.now_ns(), action=action,
            fills_added=fills_added, fills_duplicated=fills_dup,
            statement_venues=list(statement_venues), run_kind=run.kind))
        cap = self.cfg.retention.max_runs_per_date_history
        if len(revs) > cap:
            del revs[: len(revs) - cap]

    # ------------------------------------------------------------------
    # POST /settle
    # ------------------------------------------------------------------

    def settle(self, body: Any) -> Dict[str, Any]:
        """Apply one settlement submission to a run.

        ``body`` is the parsed JSON object with (after validation) shape::

            {"date": "YYYY-MM-DD",
             "fills": [ {...}, ... ],
             "statement_lines": [ {...}, ... ]}

        Returns the updated run summary plus the per-call accounting.
        """
        if not isinstance(body, dict):
            raise InvalidSettleRequest("The settle body must be a JSON object.",
                                       context={"field": "$"})
        date = parse_settlement_date(body.get("date"), self.clock)
        now = self.clock.now_ns()

        fills_raw = body.get("fills", [])
        if fills_raw is None:
            fills_raw = []
        if not isinstance(fills_raw, list):
            raise InvalidSettleRequest("The 'fills' field must be an array of fill objects.",
                                       context={"field": "fills"})
        fills = [parse_fill(obj, i) for i, obj in enumerate(fills_raw)]

        stmt_raw = body.get("statement_lines", [])
        if stmt_raw is None:
            stmt_raw = []
        if not isinstance(stmt_raw, list):
            raise InvalidSettleRequest("The 'statement_lines' field must be an array.",
                                       context={"field": "statement_lines"})
        per_venue_cap = self.cfg.matching.max_statement_lines_per_venue
        stmt_counts: Dict[str, int] = {}
        for obj in stmt_raw:
            v = obj.get("venue") if isinstance(obj, dict) else None
            if isinstance(v, str):
                stmt_counts[v] = stmt_counts.get(v, 0) + 1
        for v, n in stmt_counts.items():
            if n > per_venue_cap:
                raise InvalidSettleRequest(
                    f"Venue {v!r} has {n} statement lines in one submission; cap is {per_venue_cap}.",
                    context={"field": "statement_lines", "venue": v, "count": n, "cap": per_venue_cap})
        statements = [parse_statement_line(obj, i, seq=i) for i, obj in enumerate(stmt_raw)]

        with self._lock:
            run = self._runs.get(date)
            if run is None:
                run = SettlementRun(date=date, created_ns=now)
                self._runs[date] = run
                self._revisions.setdefault(date, [])
            self._check_open(run)

            # -- idempotent fills ---------------------------------------
            added = 0
            dups = 0
            seen_ids = run._fill_digests
            for f in fills:
                digest = hashlib.sha256(
                    canonical_json(f.__dict__).encode("utf-8")).hexdigest()
                prior = seen_ids.get(f.fill_id)
                if prior is not None:
                    if prior == digest:
                        dups += 1
                        continue
                    self._fill_conflicts += 1
                    raise FillIdConflict(
                        f"fill_id {f.fill_id!r} was already recorded with different content.",
                        context={"fill_id": f.fill_id, "date": date})
                seen_ids[f.fill_id] = digest
                run.fills.append(f)
                added += 1

            # -- statement replacement per venue ------------------------
            stmt_venues: List[str] = []
            by_venue: Dict[str, List[Any]] = {}
            for line in statements:
                by_venue.setdefault(line.venue, []).append(line)
            for venue in sorted(by_venue):
                run.statement_lines = [
                    l for l in run.statement_lines if l.venue != venue]
                run.statement_lines.extend(by_venue[venue])
                stmt_venues.append(venue)
            for venue in {l.venue for l in run.statement_lines}:
                count = sum(1 for l in run.statement_lines if l.venue == venue)
                if count > per_venue_cap:
                    raise InvalidSettleRequest(
                        f"Venue {venue!r} statement exceeds the retention bound {per_venue_cap}.",
                        context={"field": "statement_lines", "venue": venue, "count": count,
                                 "cap": per_venue_cap})

            run.updated_ns = now
            run.settle_calls += 1
            run.duplicate_fills_ignored += dups
            self._settle_calls += 1
            self._duplicate_fills += dups
            self._record_revision(date, "settle", added, dups, stmt_venues, run)

            return {
                "date": date,
                "run_kind": run.kind,
                "finalized": run.finalized,
                "fills_added": added,
                "duplicate_fills_ignored": dups,
                "statement_venues_replaced": stmt_venues,
                "run": self._run_summary(run),
            }

    # ------------------------------------------------------------------
    # EOD finalization
    # ------------------------------------------------------------------

    def finalize_eod(self, date: str) -> Dict[str, Any]:
        """Seal a run: kind -> eod, finalized -> True, content hash computed.

        Idempotent: finalizing an already-finalized run returns the existing
        seal (200) instead of raising — the operator may hit finalize twice.
        """
        date = parse_settlement_date(date, self.clock)
        now = self.clock.now_ns()
        with self._lock:
            run = self._get_run(date)
            if run.finalized:
                return {"date": date, "finalized": True, "idempotent": True,
                        "content_hash": run.content_hash, "run": self._run_summary(run)}
            run.kind = "eod"
            run.finalized = True
            run.updated_ns = now
            run.content_hash = self._seal_digest(run)
            self._finalized_runs += 1
            self._record_revision(date, "finalize", 0, 0, [], run)
            return {"date": date, "finalized": True, "idempotent": False,
                    "content_hash": run.content_hash, "run": self._run_summary(run)}

    def _seal_digest(self, run: SettlementRun) -> str:
        """Content hash over the run's canonical snapshot (tamper-evidence)."""
        snapshot = {
            "date": run.date,
            "fills": [f.__dict__ for f in run.fills],
            "statement_lines": [l.__dict__ for l in run.statement_lines],
            "net_positions": [p.to_dict() for p in compute_net_positions(run.fills, self.cfg)],
            "cash_summary": compute_cash_summary(run.fills).to_dict(),
        }
        return content_hash(snapshot)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _run_summary(self, run: SettlementRun) -> Dict[str, Any]:
        return {
            "date": run.date,
            "kind": run.kind,
            "finalized": run.finalized,
            "created_ns": run.created_ns,
            "updated_ns": run.updated_ns,
            "settle_calls": run.settle_calls,
            "duplicate_fills_ignored": run.duplicate_fills_ignored,
            "fill_count": len(run.fills),
            "statement_line_count": len(run.statement_lines),
            "content_hash": run.content_hash,
        }

    def report(self, date: str) -> Dict[str, Any]:
        """Full settlement report for one date (netting + reconciliation)."""
        date = parse_settlement_date(date, self.clock)
        with self._lock:
            run = self._get_run(date)
            discrepancies = self._discrepancies(run)
            positions = compute_net_positions(run.fills, self.cfg)
            cash = compute_cash_summary(run.fills)
            revs = self._revisions.get(date, [])
            return {
                "run": self._run_summary(run),
                "net_positions": [p.to_dict() for p in positions],
                "cash_summary": cash.to_dict(),
                "discrepancies": [d.to_dict() for d in discrepancies],
                "discrepancy_count": len(discrepancies),
                "revisions": [
                    {"seq": r.seq, "ts_ns": r.ts_ns, "action": r.action,
                     "fills_added": r.fills_added, "fills_duplicated": r.fills_duplicated,
                     "statement_venues": r.statement_venues, "run_kind": r.run_kind}
                    for r in revs],
                "verify_hash": self._seal_digest(run) if run.finalized else None,
            }

    def _discrepancies(self, run: SettlementRun) -> List[Any]:
        m = self.cfg.matching
        return matching_report(
            run.fills, run.statement_lines, run.date, self.clock.now_ns(),
            qty_tolerance=m.qty_tolerance, price_tolerance_abs=m.price_tolerance_abs)

    def discrepancies(self, date: Optional[str], venue: Optional[str],
                      kind: Optional[str], limit_raw: Optional[str]) -> Dict[str, Any]:
        """Open discrepancies, newest date first; optional filters + bounded limit."""
        with self._lock:
            if date is not None:
                date = parse_settlement_date(date, self.clock)
                run = self._get_run(date)
                pool = [(run.date, self._discrepancies(run))]
            else:
                pool = [(d, self._discrepancies(self._runs[d])) for d in sorted(self._runs, reverse=True)]
            rows = []
            for d, found in pool:
                for disc in found:
                    if venue is not None and disc.venue != venue.upper():
                        continue
                    if kind is not None and disc.kind != kind.upper():
                        continue
                    rows.append(disc.to_dict())
        if limit_raw is None:
            limit = 100
        else:
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                from .errors import InvalidLimitParam
                raise InvalidLimitParam("The limit query parameter must be a positive integer.",
                                        context={"raw": str(limit_raw)[:64]})
            if limit < 1:
                from .errors import InvalidLimitParam
                raise InvalidLimitParam("The limit query parameter must be a positive integer.",
                                        context={"raw": str(limit_raw)[:64]})
        rows = rows[:limit]
        cap = self.cfg.retention.max_discrepancy_lines
        if len(rows) > cap:
            rows = rows[:cap]
        return {"discrepancies": rows, "count": len(rows), "truncated": len(rows) >= limit}

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            dates = sorted(self._runs)
            cap = self.cfg.retention.max_report_dates
            if len(dates) > cap:
                dates = dates[-cap:]  # retention keeps the most recent dates
            return {
                "run_count": len(self._runs),
                "finalized_runs": sum(1 for r in self._runs.values() if r.finalized),
                "settle_calls": self._settle_calls,
                "duplicate_fills_ignored": self._duplicate_fills,
                "fill_id_conflicts": self._fill_conflicts,
                "finalization_conflicts": self._finalization_conflicts,
                "dates": dates,
                "runs": {d: self._run_summary(self._runs[d]) for d in dates},
            }

    # ------------------------------------------------------------------
    # Ingest (S6 position pull -> settle)
    # ------------------------------------------------------------------

    def ingest_from_positions(self, positions: Any) -> Dict[str, Any]:
        """Convert an S6 ``/positions`` payload into fills and settle them.

        S6 positions carry ``realized_pnl`` and average-cost state but not the
        day's fill history, so the ingest path derives **synthetic day-close
        fills** from each position row's ``net_qty`` and ``avg_price``: a
        single BUY (net>0) or SELL (net<0) fill per (account, symbol) whose id
        is deterministic in (date, account, symbol).  Idempotency then makes
        repeated pulls safe — the same snapshot yields the same fill ids and
        is folded to ``duplicate_fills_ignored``.
        """
        if not isinstance(positions, list):
            raise InvalidSettleRequest("The positions payload must be a JSON array.",
                                       context={"field": "positions"})
        fills = []
        import datetime as _dt
        today = self.clock.today_date()
        # The synthetic fill carries a date-deterministic stamp (UTC midnight
        # of the settlement date), NOT the wall clock: repeated pulls of an
        # unchanged snapshot must yield byte-identical fills so idempotency
        # folds them to duplicates.  A CHANGED position reuses the id with new
        # content and is rejected as STL-205 (no silent rewrites of a run).
        day_ts_ns = int(_dt.datetime.strptime(today, "%Y-%m-%d")
                        .replace(tzinfo=_dt.timezone.utc).timestamp() * 1e9)
        for i, row in enumerate(positions):
            if not isinstance(row, dict):
                raise InvalidSettleRequest(f"positions[{i}] must be a JSON object.",
                                           context={"field": f"positions[{i}]"})
            symbol = row.get("symbol")
            account = row.get("account", "MAIN")
            net_qty = row.get("net_qty", 0)
            avg_price = row.get("avg_price", 0.0)
            if not isinstance(symbol, str) or not (1 <= len(symbol) <= 64):
                raise InvalidSettleRequest(f"positions[{i}].symbol is invalid.",
                                           context={"field": f"positions[{i}].symbol"})
            if isinstance(net_qty, bool) or not isinstance(net_qty, int) or net_qty == 0:
                continue  # flat positions contribute no settlement fill
            if isinstance(avg_price, bool) or not isinstance(avg_price, (int, float)) or avg_price < 0:
                raise InvalidSettleRequest(f"positions[{i}].avg_price is invalid.",
                                           context={"field": f"positions[{i}].avg_price"})
            venue = row.get("venue") if isinstance(row.get("venue"), str) else "INTERNAL"
            fills.append({
                "fill_id": f"ING-{today}-{str(account).upper()}-{symbol.upper()}",
                "ts_ns": day_ts_ns,
                "venue": venue,
                "symbol": symbol,
                "side": "BUY" if net_qty > 0 else "SELL",
                "px": float(avg_price),
                "qty": abs(net_qty),
                "fee": 0.0,
                "account": str(account).upper(),
            })
        if not fills:
            return {"settled_fills": 0, "date": self.clock.today_date()}
        return self.settle({"date": self.clock.today_date(), "fills": fills})
