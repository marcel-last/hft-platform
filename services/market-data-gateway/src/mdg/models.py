"""market_data_gateway — core domain models.

This module defines the wire and in-memory representations of everything that
flows through the gateway: raw venue messages, normalized quotes, trade prints,
control events, sequence tracking state and quality metrics.

Design rules
------------
* Every model is a plain ``dataclass`` (value semantics, cheap to copy).
* Timestamps are always nanoseconds since the Unix epoch (int64) so that no
  floating point arithmetic ever touches the hot path.
* All enums use string values matching the JSON wire format used by the other
  14 services in the platform.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def now_ns() -> int:
    """Current wall-clock time in nanoseconds since the Unix epoch."""
    return time.time_ns()


def ms_to_ns(ms: float) -> int:
    return int(ms * 1_000_000)


def ns_to_ms(ns: int) -> float:
    return ns / 1_000_000


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Side(str, enum.Enum):
    """Buy/sell side of a quote level or trade."""

    BID = "BID"
    ASK = "ASK"
    LAST = "LAST"
    UNKNOWN = "UNKNOWN"


class QuoteAction(str, enum.Enum):
    """ITCH-style market data action codes (subset)."""

    NEW = "N"               # new quote level
    MODIFY = "M"            # modify existing level
    DELETE = "D"            # delete level
    EXECUTE = "E"           # trade executed at this price
    BIDDING_TICK = "B"      # bidding tick (aggressive bid)
    OFFERING_TICK = "O"     # offering tick (aggressive ask)


class VenueStatus(str, enum.Enum):
    """Connection state of a venue feed."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATING = "AUTHENTICATING"
    SUBSCRIBING = "SUBSCRIBING"
    STREAMING = "STREAMING"
    RECONNECTING = "RECONNECTING"
    SUSPECT = "SUSPECT"
    DEAD = "DEAD"


class DataQuality(str, enum.Enum):
    """Per-quote quality flags assigned by the quality monitor."""

    FRESH = "FRESH"          # within staleness budget
    STALE_WARN = "STALE_WARN"  # approaching staleness limit
    STALE = "STALE"          # beyond staleness limit
    GAP_SUSPECT = "GAP_SUSPECT"  # possible sequence gap upstream
    OUT_OF_ORDER = "OUT_OF_ORDER"


class ControlEventKind(str, enum.Enum):
    """Control-plane events emitted by the gateway."""

    VENUE_CONNECTED = "VENUE_CONNECTED"
    VENUE_DISCONNECTED = "VENUE_DISCONNECTED"
    SUBSCRIPTION_ACK = "SUBSCRIPTION_ACK"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    SEQUENCE_RESYNC = "SEQUENCE_RESYNC"
    STALENESS_BREACH = "STALENESS_BREACH"
    HEARTBEAT_MISSED = "HEARTBEAT_MISSED"
    SNAPSHOT_REQUESTED = "SNAPSHOT_REQUESTED"
    SNAPSHOT_RECEIVED = "SNAPSHOT_RECEIVED"
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"
    CIRCUIT_BREAKER_CLOSED = "CIRCUIT_BREAKER_CLOSED"


# ---------------------------------------------------------------------------
# Raw venue messages (pre-normalization)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RawQuote:
    """A quote level as received from a venue, before normalization.

    Prices/quantities are kept in the venue's native representation
    (price = integer ticks * tick_size applied later; here we store the
    raw price already scaled to decimal units).
    """

    venue_id: str
    symbol_venue: str          # venue-local symbol, e.g. "FESX"
    seq_no: int                # venue sequence number for this message
    msg_type: QuoteAction      # parsed action code
    side: Side
    price: float               # decimal price in the instrument's currency unit
    quantity: int              # lots (integer) at this level
    depth_level: int           # 1 = top of book, 2 = second level, ...
    venue_timestamp_ns: int    # exchange timestamp (ns since epoch)
    receive_timestamp_ns: int  # gateway receive timestamp (ns since epoch)


@dataclass(slots=True)
class RawTrade:
    """A trade print as received from a venue."""

    venue_id: str
    symbol_venue: str
    seq_no: int
    price: float
    quantity: int
    aggressor_side: Side       # who lifted the offer / hit the bid
    exec_id: str               # venue execution id (string, opaque)
    venue_timestamp_ns: int
    receive_timestamp_ns: int


@dataclass(slots=True)
class RawHeartbeat:
    """A keep-alive message from a venue connection."""

    venue_id: str
    seq_no: int
    receive_timestamp_ns: int


# ---------------------------------------------------------------------------
# Normalized models (post symbol-map, post tick validation)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Quote:
    """A normalized quote level ready for distribution to consumers.

    This is the canonical in-memory representation used by every downstream
    service (order book builder, latency monitor, data quality monitor, ...).
    """

    canonical_symbol: str
    venue_id: str
    seq_no: int
    action: QuoteAction
    side: Side
    price: float
    quantity: int
    depth_level: int
    tick_size: float           # copied from normalization table for convenience
    quality: DataQuality       # assigned by the quality monitor
    venue_timestamp_ns: int
    receive_timestamp_ns: int
    normalize_latency_ns: int  # gateway processing latency (receive -> emit)


@dataclass(slots=True)
class TradePrint:
    """A normalized trade print."""

    canonical_symbol: str
    venue_id: str
    seq_no: int
    price: float
    quantity: int
    aggressor_side: Side
    exec_id: str
    tick_size: float
    quality: DataQuality
    venue_timestamp_ns: int
    receive_timestamp_ns: int
    normalize_latency_ns: int


@dataclass(slots=True)
class ControlEvent:
    """A control-plane event broadcast to subscribers."""

    kind: ControlEventKind
    venue_id: str
    detail: str                # free-form, human readable
    sequence_gap_size: Optional[int] = None
    timestamp_ns: int = field(default_factory=now_ns)


# ---------------------------------------------------------------------------
# Sequence tracking state (per venue / per symbol)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SequenceTracker:
    """Tracks the expected venue sequence number for one (venue, symbol)."""

    venue_id: str
    symbol_venue: str
    last_seq_no: int = 0
    gap_count: int = 0             # consecutive gaps seen since last resync
    total_gaps: int = 0            # lifetime counter
    max_gap_size_seen: int = 0
    last_gap_at_ns: int = 0
    resync_count: int = 0

    def observe(self, seq_no: int, now: int) -> Optional[int]:
        """Feed a new sequence number.

        Returns the gap size if a gap was detected (``seq_no - expected > 1``),
        ``None`` if the message is in order, and a *negative* value if the
        message is out of order (older than expected).
        """
        expected = self.last_seq_no + 1
        delta = seq_no - expected
        if delta == 0:
            self.last_seq_no = seq_no
            return None
        if delta > 0:
            # forward gap
            self.gap_count += 1
            self.total_gaps += 1
            self.max_gap_size_seen = max(self.max_gap_size_seen, delta)
            self.last_gap_at_ns = now
            self.last_seq_no = seq_no
            return delta
        # negative delta => out-of-order (retransmission or clock skew)
        self.gap_count += 1
        self.last_seq_no = max(self.last_seq_no, seq_no)
        return delta


# ---------------------------------------------------------------------------
# Order book snapshot (built by the order-book-builder service, mirrored here
# for type sharing)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BookLevel:
    price: float
    quantity: int
    depth_level: int


@dataclass(slots=True)
class OrderBookSnapshot:
    """Top-of-book + full L2 snapshot at a point in time."""

    canonical_symbol: str
    venue_id: str
    bids: List[BookLevel] = field(default_factory=list)   # sorted price desc
    asks: List[BookLevel] = field(default_factory=list)   # sorted price asc
    best_bid_price: float = 0.0
    best_bid_qty: int = 0
    best_ask_price: float = 0.0
    best_ask_qty: int = 0
    mid_price: float = 0.0
    spread: float = 0.0
    timestamp_ns: int = field(default_factory=now_ns)

    def recompute_top(self) -> None:
        """Recompute top-of-book fields from the level lists."""
        if self.bids:
            self.best_bid_price = self.bids[0].price
            self.best_bid_qty = self.bids[0].quantity
        else:
            self.best_bid_price = 0.0
            self.best_bid_qty = 0
        if self.asks:
            self.best_ask_price = self.asks[0].price
            self.best_ask_qty = self.asks[0].quantity
        else:
            self.best_ask_price = 0.0
            self.best_ask_qty = 0
        if self.best_bid_price > 0.0 and self.best_ask_price > 0.0:
            self.mid_price = (self.best_bid_price + self.best_ask_price) / 2.0
            self.spread = self.best_ask_price - self.best_bid_price
        else:
            self.mid_price = 0.0
            self.spread = 0.0


# ---------------------------------------------------------------------------
# Quality metrics (aggregated per symbol window)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class QualityMetrics:
    """Rolling quality metrics for one canonical symbol."""

    canonical_symbol: str
    window_start_ns: int
    quotes_seen: int = 0
    stale_quotes: int = 0
    gap_events: int = 0
    out_of_order_events: int = 0
    max_staleness_ms_observed: float = 0.0
    avg_normalize_latency_ns: float = 0.0

    def record(self, quote: Quote, staleness_ms: float) -> None:
        self.quotes_seen += 1
        if quote.quality in (DataQuality.STALE, DataQuality.STALE_WARN):
            self.stale_quotes += 1
        if quote.quality == DataQuality.GAP_SUSPECT:
            self.gap_events += 1
        if quote.quality == DataQuality.OUT_OF_ORDER:
            self.out_of_order_events += 1
        self.max_staleness_ms_observed = max(self.max_staleness_ms_observed, staleness_ms)
        # running average (Welford-free simple mean is fine at this scale)
        n = self.quotes_seen
        prev_avg = self.avg_normalize_latency_ns * (n - 1)
        self.avg_normalize_latency_ns = (prev_avg + quote.normalize_latency_ns) / n

    def staleness_pct(self) -> float:
        if self.quotes_seen == 0:
            return 0.0
        return 100.0 * self.stale_quotes / self.quotes_seen


# ---------------------------------------------------------------------------
# Wire-format helpers (JSON serialization for the distribution layer)
# ---------------------------------------------------------------------------

def quote_to_wire(q: Quote) -> Dict[str, object]:
    """Serialize a normalized quote to its JSON wire dict."""
    return {
        "v": 1,                                   # wire format version
        "sym": q.canonical_symbol,
        "ven": q.venue_id,
        "seq": q.seq_no,
        "act": q.action.value,
        "side": q.side.value,
        "px": q.price,
        "qty": q.quantity,
        "lvl": q.depth_level,
        "tick": q.tick_size,
        "q": q.quality.value,
        "vt": q.venue_timestamp_ns,
        "rt": q.receive_timestamp_ns,
        "nl": q.normalize_latency_ns,
    }


def trade_to_wire(t: TradePrint) -> Dict[str, object]:
    return {
        "v": 1,
        "sym": t.canonical_symbol,
        "ven": t.venue_id,
        "seq": t.seq_no,
        "px": t.price,
        "qty": t.quantity,
        "aggr": t.aggressor_side.value,
        "xid": t.exec_id,
        "tick": t.tick_size,
        "q": t.quality.value,
        "vt": t.venue_timestamp_ns,
        "rt": t.receive_timestamp_ns,
        "nl": t.normalize_latency_ns,
    }


def control_event_to_wire(e: ControlEvent) -> Dict[str, object]:
    return {
        "v": 1,
        "kind": e.kind.value,
        "ven": e.venue_id,
        "detail": e.detail,
        "gap": e.sequence_gap_size,
        "ts": e.timestamp_ns,
    }


def book_snapshot_to_wire(s: OrderBookSnapshot) -> Dict[str, object]:
    return {
        "v": 1,
        "sym": s.canonical_symbol,
        "ven": s.venue_id,
        "bids": [[lvl.price, lvl.quantity, lvl.depth_level] for lvl in s.bids],
        "asks": [[lvl.price, lvl.quantity, lvl.depth_level] for lvl in s.asks],
        "bb_px": s.best_bid_price,
        "bb_qty": s.best_bid_qty,
        "ba_px": s.best_ask_price,
        "ba_qty": s.best_ask_qty,
        "mid": s.mid_price,
        "sprd": s.spread,
        "ts": s.timestamp_ns,
    }
