"""data_quality_monitor — core domain models.

Models in this module describe the unified data-quality view maintained by the
monitor (S8):

* :class:`QualityState` / :class:`Severity` — enums,
* :class:`FeedMetrics`   — normalized per-symbol metrics pulled from S1,
* :class:`BookHealthRow` — normalized per-(symbol, venue) book health from S2,
* :class:`SymbolQuality` — the aggregated per-symbol quality state with a
  composite score and degradation status,
* :class:`GapRecord`     — one feed-gap (silence) or sequence-gap event,
* :class:`StalenessSample` — one staleness reading for a symbol,
* :class:`DegradationEvent` — one degradation episode (enter/recover),

plus the ``now_ns()`` hot-path timestamp helper.  All timestamps are int64
nanoseconds since the Unix epoch; no floating point is used in any latency or
score arithmetic (scores are integers).
"""

from __future__ import annotations

import enum
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def now_ns() -> int:
    """Current time as int64 nanoseconds since the Unix epoch (hot-path convention)."""
    return time.time_ns()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class QualityState(str, enum.Enum):
    """Aggregated per-symbol data-quality state."""

    OK = "OK"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"      # no observations yet (no upstream data)


class Severity(str, enum.Enum):
    """Severity of a degradation episode."""

    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


class DegradationKind(str, enum.Enum):
    """What happened: the symbol degraded or it recovered."""

    DEGRADED = "DEGRADED"
    RECOVERED = "RECOVERED"


# ---------------------------------------------------------------------------
# Normalized upstream rows
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FeedMetrics:
    """Per-symbol feed quality as reported by the market-data gateway (S1).

    ``silent_ms`` is the wall-clock time since the last quote for this symbol;
    a value above the configured silence budget marks the feed as gapped.
    ``max_staleness_ms_observed`` is the worst per-quote venue->receive age seen
    in the S1 rolling window.
    """

    symbol: str
    quotes_seen: int = 0
    stale_pct: float = 0.0            # 0..100 (percent of quotes tagged STALE)
    gap_events: int = 0               # sequence gaps observed in the S1 window
    out_of_order_events: int = 0      # out-of-order sequence events
    max_staleness_ms_observed: float = 0.0
    silent_ms: float = 0.0            # time since last quote for this symbol

    @classmethod
    def from_dict(cls, symbol: str, row: Dict[str, Any]) -> "FeedMetrics":
        """Parse one entry of the S1 ``/quality`` report's ``symbols`` map."""
        return cls(
            symbol=symbol,
            quotes_seen=int(row.get("quotes_seen", 0) or 0),
            stale_pct=float(row.get("stale_pct", 0.0) or 0.0),
            gap_events=int(row.get("gap_events", 0) or 0),
            out_of_order_events=int(row.get("out_of_order_events", 0) or 0),
            max_staleness_ms_observed=float(
                row.get("max_staleness_ms_observed", 0.0) or 0.0
            ),
            silent_ms=float(row.get("silent_ms", 0.0) or 0.0),
        )


@dataclass(slots=True)
class BookHealthRow:
    """Per-(symbol, venue) book health as reported by the order-book-builder (S2)."""

    symbol: str
    venue: str
    health: str                        # HEALTHY | STALE | REBUILDING | EMPTY
    cross_events: int = 0              # lifetime cross-detection counter for this book
    messages_applied: int = 0
    rebuilds: int = 0

    @classmethod
    def from_dict(cls, row: Dict[str, Any]) -> "BookHealthRow":
        return cls(
            symbol=str(row.get("symbol", "")),
            venue=str(row.get("venue", "")),
            health=str(row.get("health", "EMPTY")),
            cross_events=int(row.get("cross_events", 0) or 0),
            messages_applied=int(row.get("messages_applied", 0) or 0),
            rebuilds=int(row.get("rebuilds", 0) or 0),
        )


# ---------------------------------------------------------------------------
# Aggregated per-symbol state
# ---------------------------------------------------------------------------

@dataclass
class SymbolQuality:
    """Aggregated, scored data-quality state for one canonical symbol.

    The composite ``score`` is an integer in ``[0, 100]`` (100 = perfect) and is
    recomputed on every aggregation pass from the latest S1 feed metrics and S2
    book health.  ``state`` transitions with hysteresis (see
    :class:`~dqm.config.ScoringConfig`) so a score oscillating around the
    degraded boundary does not flap.
    """

    symbol: str
    state: QualityState = QualityState.UNKNOWN
    score: int = 100
    feed_silent: bool = False          # S1 reports the symbol as silent (gap)
    book_stale_venues: List[str] = field(default_factory=list)
    book_cross_events: int = 0         # sum of cross events across venues
    book_health_by_venue: Dict[str, str] = field(default_factory=dict)
    stale_pct: float = 0.0
    gap_events: int = 0
    out_of_order_events: int = 0
    max_staleness_ms_observed: float = 0.0
    silent_ms: float = 0.0
    quotes_seen: int = 0
    reasons: List[str] = field(default_factory=list)
    last_updated_ns: int = field(default_factory=now_ns)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "score": self.score,
            "feed_silent": self.feed_silent,
            "book_stale_venues": list(self.book_stale_venues),
            "book_cross_events": self.book_cross_events,
            "book_health_by_venue": dict(self.book_health_by_venue),
            "stale_pct": round(self.stale_pct, 4),
            "gap_events": self.gap_events,
            "out_of_order_events": self.out_of_order_events,
            "max_staleness_ms_observed": round(self.max_staleness_ms_observed, 3),
            "silent_ms": round(self.silent_ms, 1),
            "quotes_seen": self.quotes_seen,
            "reasons": list(self.reasons),
            "last_updated_ns": self.last_updated_ns,
        }


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class GapRecord:
    """One feed-gap (silence) or sequence-gap observation for a symbol."""

    symbol: str
    kind: str                          # "FEED_SILENCE" | "SEQUENCE_GAP"
    detail: str
    magnitude_ms: float = 0.0          # silence duration (ms) for FEED_SILENCE
    ts_ns: int = field(default_factory=now_ns)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "detail": self.detail,
            "magnitude_ms": round(self.magnitude_ms, 1),
            "ts_ns": self.ts_ns,
        }


@dataclass(slots=True)
class StalenessSample:
    """One staleness reading for a symbol (worst observed + current silence)."""

    symbol: str
    max_staleness_ms: float = 0.0      # worst per-quote age in the S1 window
    silent_ms: float = 0.0             # time since last quote
    stale_pct: float = 0.0             # percent of quotes tagged STALE
    ts_ns: int = field(default_factory=now_ns)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "max_staleness_ms": round(self.max_staleness_ms, 3),
            "silent_ms": round(self.silent_ms, 1),
            "stale_pct": round(self.stale_pct, 4),
            "ts_ns": self.ts_ns,
        }


# ---------------------------------------------------------------------------
# Degradation episodes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DegradationEvent:
    """One degradation episode: a symbol entering or leaving DEGRADED state.

    ``severity`` is derived from the composite score at the moment of transition
    (see :func:`dqm.quality_engine.severity_for_score`).  ``reasons`` captures
    the contributing factors so operators can see *why* it degraded.
    """

    id: str
    symbol: str
    kind: DegradationKind
    severity: Severity
    score: int
    reasons: List[str]
    ts_ns: int = field(default_factory=now_ns)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "score": self.score,
            "reasons": list(self.reasons),
            "ts_ns": self.ts_ns,
        }


# ---------------------------------------------------------------------------
# Id generators (module-level counters)
# ---------------------------------------------------------------------------

_degradation_counter = itertools.count(1)


def next_degradation_id() -> str:
    return f"DGR-{next(_degradation_counter):08d}"
