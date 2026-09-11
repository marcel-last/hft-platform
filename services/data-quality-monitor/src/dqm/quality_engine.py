"""data_quality_monitor — aggregation & scoring core.

The :class:`QualityEngine` is the heart of S8.  On each aggregation pass it:

1.  ingests one S1 ``/quality`` report (per-symbol feed metrics) and one S2
    ``/books`` listing (per-(symbol, venue) book health);
2.  merges them into a single :class:`~dqm.models.SymbolQuality` per canonical
    symbol (the union of symbols seen by either upstream);
3.  recomputes the composite quality score and applies hysteresis so the
    DEGRADED state does not flap;
4.  records degradation/recovery episodes into a bounded history and returns
    the newly-entered degradations so the caller can fan them out to S10.

All scoring is integer arithmetic over ``[0, 100]``; the only floats are the
upstream-reported percentages/milliseconds which are compared against budgets,
never used in latency math.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG, ServiceConfig
from .models import (
    BookHealthRow,
    DegradationEvent,
    DegradationKind,
    FeedMetrics,
    GapRecord,
    QualityState,
    Severity,
    StalenessSample,
    SymbolQuality,
    next_degradation_id,
    now_ns,
)

logger = logging.getLogger("dqm.quality_engine")


def severity_for_score(score: int) -> Severity:
    """Map a composite score to a degradation severity (integer thresholds)."""
    if score < 40:
        return Severity.CRITICAL
    if score < 60:
        return Severity.MAJOR
    return Severity.MINOR


class QualityEngine:
    """Thread-safe aggregator of S1 + S2 quality metrics into a unified view."""

    def __init__(self, cfg: Optional[ServiceConfig] = None) -> None:
        self.cfg = cfg or CONFIG
        self._symbols: Dict[str, SymbolQuality] = {}
        self._degradations: List[DegradationEvent] = []
        self._lock = threading.Lock()
        # lifetime counters
        self.passes_completed = 0
        self.upstream_failures = 0
        self.degradations_total = 0
        self.last_pass_ns = 0

    # ------------------------------------------------------------------
    # Ingestion + scoring (the core pass)
    # ------------------------------------------------------------------

    def ingest_and_score(
        self,
        feed_report: Optional[Dict[str, Any]],
        book_rows: List[BookHealthRow],
    ) -> List[DegradationEvent]:
        """Ingest one S1 report + one S2 books listing; return new degradations.

        ``feed_report`` is the raw body of S1 ``GET /quality`` (a dict with a
        ``symbols`` map).  ``book_rows`` are already-normalized S2 rows.  Both
        may be empty/None (upstream down) — in that case nothing is scored and
        no state transitions happen, so a transient upstream outage does not
        flap symbols to DEGRADED.
        """
        feed_map = {}
        if isinstance(feed_report, dict):
            raw = feed_report.get("symbols")
            if isinstance(raw, dict):
                for sym, row in raw.items():
                    if isinstance(row, dict):
                        feed_map[sym] = FeedMetrics.from_dict(sym, row)

        # group book rows by symbol
        books_by_symbol: Dict[str, List[BookHealthRow]] = {}
        for row in book_rows:
            if row.symbol:
                books_by_symbol.setdefault(row.symbol, []).append(row)

        symbols = set(feed_map) | set(books_by_symbol)
        if not symbols:
            return []

        new_degradations: List[DegradationEvent] = []
        th = self.cfg.thresholds
        sc = self.cfg.scoring

        with self._lock:
            for symbol in sorted(symbols):
                feed = feed_map.get(symbol)
                books = books_by_symbol.get(symbol, [])
                sq = self._symbols.get(symbol)
                if sq is None:
                    sq = SymbolQuality(symbol=symbol)
                    self._symbols[symbol] = sq

                # -- gather metrics ----------------------------------------
                stale_pct = feed.stale_pct if feed else 0.0
                gap_events = feed.gap_events if feed else 0
                out_of_order = feed.out_of_order_events if feed else 0
                max_staleness_ms = feed.max_staleness_ms_observed if feed else 0.0
                silent_ms = feed.silent_ms if feed else 0.0
                quotes_seen = feed.quotes_seen if feed else 0

                book_stale_venues: List[str] = []
                book_cross_events = 0
                health_by_venue: Dict[str, str] = {}
                for b in books:
                    health_by_venue[b.venue] = b.health
                    book_cross_events += b.cross_events
                    if b.health == "STALE":
                        book_stale_venues.append(b.venue)

                # -- penalty + reasons (integer score math) -----------------
                penalty = 0
                reasons: List[str] = []

                feed_silent = silent_ms > th.max_silent_ms
                if feed_silent:
                    penalty += int(th.gap_weight * 10)
                    reasons.append(f"feed silent for {silent_ms:.0f} ms")

                if stale_pct > th.stale_pct_crit:
                    penalty += int((stale_pct - th.stale_pct_warn) * th.stale_pct_weight)
                    reasons.append(f"{stale_pct:.1f}% of quotes STALE (crit budget {th.stale_pct_crit}%)")
                elif stale_pct > th.stale_pct_warn:
                    penalty += int(stale_pct)
                    reasons.append(f"{stale_pct:.1f}% of quotes STALE (warn budget {th.stale_pct_warn}%)")

                if gap_events > 0:
                    penalty += min(gap_events, 25)
                    reasons.append(f"{gap_events} sequence gaps in window")

                if out_of_order > 0:
                    penalty += min(out_of_order, 10)
                    reasons.append(f"{out_of_order} out-of-order events")

                if max_staleness_ms > th.max_staleness_ms:
                    penalty += int((max_staleness_ms - th.max_staleness_ms) // 50) + 2
                    reasons.append(
                        f"worst quote age {max_staleness_ms:.0f} ms > budget {th.max_staleness_ms} ms"
                    )

                if book_stale_venues:
                    penalty += len(book_stale_venues) * 15
                    reasons.append(f"stale books on {', '.join(sorted(book_stale_venues))}")

                if book_cross_events > th.cross_event_budget:
                    penalty += (book_cross_events - th.cross_event_budget) * 2
                    reasons.append(
                        f"{book_cross_events} cross events > budget {th.cross_event_budget}"
                    )

                score = max(0, min(100, 100 - penalty))

                # -- hysteresis state transition ---------------------------
                prev_state = sq.state
                if prev_state == QualityState.UNKNOWN:
                    new_state = (
                        QualityState.DEGRADED
                        if score < sc.degraded_below
                        else QualityState.OK
                    )
                elif prev_state == QualityState.DEGRADED:
                    new_state = (
                        QualityState.OK if score >= sc.recovered_at_or_above else QualityState.DEGRADED
                    )
                else:  # OK
                    new_state = (
                        QualityState.DEGRADED if score < sc.degraded_below else QualityState.OK
                    )

                # -- persist metrics ---------------------------------------
                sq.feed_silent = feed_silent
                sq.book_stale_venues = book_stale_venues
                sq.book_cross_events = book_cross_events
                sq.book_health_by_venue = health_by_venue
                sq.stale_pct = stale_pct
                sq.gap_events = gap_events
                sq.out_of_order_events = out_of_order
                sq.max_staleness_ms_observed = max_staleness_ms
                sq.silent_ms = silent_ms
                sq.quotes_seen = quotes_seen
                sq.score = score
                sq.reasons = reasons
                sq.last_updated_ns = now_ns()

                # -- record transitions ------------------------------------
                if new_state != prev_state and new_state is not QualityState.UNKNOWN:
                    if new_state == QualityState.DEGRADED:
                        event = DegradationEvent(
                            id=next_degradation_id(),
                            symbol=symbol,
                            kind=DegradationKind.DEGRADED,
                            severity=severity_for_score(score),
                            score=score,
                            reasons=list(reasons),
                        )
                        self._degradations.append(event)
                        if len(self._degradations) > self.cfg.alerting.max_degradations:
                            del self._degradations[: len(self._degradations) - self.cfg.alerting.max_degradations]
                        self.degradations_total += 1
                        new_degradations.append(event)
                        logger.warning(
                            "symbol %s DEGRADED (score=%d, severity=%s): %s",
                            symbol, score, event.severity.value, "; ".join(reasons),
                        )
                    else:
                        event = DegradationEvent(
                            id=next_degradation_id(),
                            symbol=symbol,
                            kind=DegradationKind.RECOVERED,
                            severity=Severity.MINOR,
                            score=score,
                            reasons=[f"recovered to score {score}"],
                        )
                        self._degradations.append(event)
                        if len(self._degradations) > self.cfg.alerting.max_degradations:
                            del self._degradations[: len(self._degradations) - self.cfg.alerting.max_degradations]
                        logger.info("symbol %s RECOVERED (score=%d)", symbol, score)

                sq.state = new_state

        self.passes_completed += 1
        self.last_pass_ns = now_ns()
        return new_degradations

    # ------------------------------------------------------------------
    # Read views
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """Full dashboard including engine counters (no lock re-entry)."""
        with self._lock:
            rows = [sq.to_dict() for sq in self._symbols.values()]
            counts: Dict[str, int] = {"OK": 0, "DEGRADED": 0, "UNKNOWN": 0}
            for sq in self._symbols.values():
                counts[sq.state.value] += 1
            stats = self.stats_view_locked()
        rows.sort(key=lambda r: (r["state"] != "DEGRADED", -r["score"], r["symbol"]))
        return {
            "ts_ns": now_ns(),
            "symbols_total": len(rows),
            "counts": counts,
            "degraded_symbols": [r["symbol"] for r in rows if r["state"] == "DEGRADED"],
            "symbols": rows,
            "engine": stats,
        }

    def symbol_view(self, symbol: str) -> Optional[SymbolQuality]:
        """Return a deep copy of one symbol's state (safe to mutate by callers)."""
        with self._lock:
            sq = self._symbols.get(symbol)
            if sq is None:
                return None
            return SymbolQuality(
                symbol=sq.symbol,
                state=sq.state,
                score=sq.score,
                feed_silent=sq.feed_silent,
                book_stale_venues=list(sq.book_stale_venues),
                book_cross_events=sq.book_cross_events,
                book_health_by_venue=dict(sq.book_health_by_venue),
                stale_pct=sq.stale_pct,
                gap_events=sq.gap_events,
                out_of_order_events=sq.out_of_order_events,
                max_staleness_ms_observed=sq.max_staleness_ms_observed,
                silent_ms=sq.silent_ms,
                quotes_seen=sq.quotes_seen,
                reasons=list(sq.reasons),
                last_updated_ns=sq.last_updated_ns,
            )

    def gaps(self, limit: int = 100) -> List[GapRecord]:
        """Feed-gap and sequence-gap observations across all symbols (bounded)."""
        out: List[GapRecord] = []
        with self._lock:
            for sq in sorted(self._symbols.values(), key=lambda s: s.symbol):
                if sq.feed_silent:
                    out.append(GapRecord(
                        symbol=sq.symbol,
                        kind="FEED_SILENCE",
                        detail=f"no quotes for {sq.silent_ms:.0f} ms (budget "
                               f"{self.cfg.thresholds.max_silent_ms} ms)",
                        magnitude_ms=sq.silent_ms,
                    ))
                if sq.gap_events > 0:
                    out.append(GapRecord(
                        symbol=sq.symbol,
                        kind="SEQUENCE_GAP",
                        detail=f"{sq.gap_events} sequence gaps in the S1 window",
                        magnitude_ms=0.0,
                    ))
        # most severe first (longest silence), then by symbol
        out.sort(key=lambda g: (g.kind != "FEED_SILENCE", -g.magnitude_ms, g.symbol))
        return out[:limit]

    def staleness(self, limit: int = 100) -> List[StalenessSample]:
        """Current staleness reading per symbol (worst age + silence)."""
        out: List[StalenessSample] = []
        with self._lock:
            for sq in sorted(self._symbols.values(), key=lambda s: s.symbol):
                out.append(StalenessSample(
                    symbol=sq.symbol,
                    max_staleness_ms=sq.max_staleness_ms_observed,
                    silent_ms=sq.silent_ms,
                    stale_pct=sq.stale_pct,
                ))
        out.sort(key=lambda s: (-s.max_staleness_ms, -s.silent_ms, s.symbol))
        return out[:limit]

    def degradations(self, limit: int = 100,
                     kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Bounded degradation-episode history (newest first)."""
        with self._lock:
            events = list(reversed(self._degradations))
        if kind:
            kind = kind.upper()
            events = [e for e in events if e.kind.value == kind]
        return [e.to_dict() for e in events[:limit]]

    def readiness_reasons(self) -> List[str]:
        """Reasons the service is not ready (empty list = ready)."""
        reasons: List[str] = []
        if self.passes_completed == 0:
            reasons.append("no aggregation pass completed yet")
        return reasons

    def stats_view_locked(self) -> Dict[str, Any]:
        """Engine counters + symbol counts (call with lock held)."""
        counts: Dict[str, int] = {"OK": 0, "DEGRADED": 0, "UNKNOWN": 0}
        for sq in self._symbols.values():
            counts[sq.state.value] += 1
        return {
            "symbols_tracked": len(self._symbols),
            "state_counts": counts,
            "passes_completed": self.passes_completed,
            "upstream_failures": self.upstream_failures,
            "degradations_total": self.degradations_total,
            "last_pass_ns": self.last_pass_ns,
        }

    def stats_view(self) -> Dict[str, Any]:
        with self._lock:
            return self.stats_view_locked()
