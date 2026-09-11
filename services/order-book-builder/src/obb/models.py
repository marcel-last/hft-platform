"""order_book_builder — core domain models.

Models in this module describe the state of a maintained L2 limit order book:
individual price levels, the aggregated book per (symbol, venue), top-of-book
views, and the events emitted when the book structure changes materially.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def now_ns() -> int:
    return time.time_ns()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BookSide(str, enum.Enum):
    BID = "BID"
    ASK = "ASK"


class BookEventKind(str, enum.Enum):
    """Material book-structure changes broadcast to consumers."""

    TOP_CHANGE = "TOP_CHANGE"          # best bid or best ask price moved
    SPREAD_CHANGE = "SPREAD_CHANGE"    # spread widened/narrowed beyond threshold
    DEPTH_DRAINED = "DEPTH_DRAINED"    # a side lost more than N% of its quantity
    IMBALANCE = "IMBALANCE"            # bid/ask ratio crossed the alert threshold
    BOOK_REBUILT = "BOOK_REBUILT"      # full rebuild from snapshot completed
    BOOK_STALE = "BOOK_STALE"          # no updates within stale_book_ttl_ms
    CROSS_DETECTED = "CROSS_DETECTED"  # best_bid >= best_ask (data anomaly)


class BookHealth(str, enum.Enum):
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    REBUILDING = "REBUILDING"
    EMPTY = "EMPTY"


# ---------------------------------------------------------------------------
# Level and book models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PriceLevel:
    """One aggregated price level on one side of the book."""

    price: float
    quantity: int
    updates: int = 0                   # how many raw messages touched this level
    last_update_ns: int = field(default_factory=now_ns)


@dataclass(slots=True)
class TopOfBook:
    """Immutable top-of-book view (cheap to copy, safe to share)."""

    best_bid_price: float
    best_bid_qty: int
    best_ask_price: float
    best_ask_qty: int
    mid_price: float
    spread: float
    spread_ticks: int
    imbalance_ratio: float             # bid_qty / ask_qty (inf-safe)
    timestamp_ns: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "bb_px": self.best_bid_price,
            "bb_qty": self.best_bid_qty,
            "ba_px": self.best_ask_price,
            "ba_qty": self.best_ask_qty,
            "mid": self.mid_price,
            "spread": self.spread,
            "spread_ticks": self.spread_ticks,
            "imb": round(self.imbalance_ratio, 6),
            "ts": self.timestamp_ns,
        }


@dataclass(slots=True)
class BookEvent:
    """A material change event for one book."""

    kind: BookEventKind
    canonical_symbol: str
    venue_id: str
    detail: str
    timestamp_ns: int = field(default_factory=now_ns)
    top: Optional[TopOfBook] = None


@dataclass
class OrderBook:
    """A maintained L2 book for one (canonical_symbol, venue).

    Note: this dataclass intentionally omits ``slots=True`` because the engine
    attaches cached sorted-view attributes (``_sorted_bids_cache`` /
    ``_sorted_asks_cache``) dynamically via ``setattr``; slots would forbid
    any attribute not declared up front.

    ``bids``/``asks`` map price -> :class:`PriceLevel`.  Sorted views are
    computed on demand and cached until the next mutation invalidates them.
    All mutations go through :class:`~obb.book_engine.BookEngine` so that
    invariants (sortedness, non-negative quantities) hold everywhere.
    """

    canonical_symbol: str
    venue_id: str
    tick_size: float
    bids: Dict[float, PriceLevel] = field(default_factory=dict)   # price -> level
    asks: Dict[float, PriceLevel] = field(default_factory=dict)
    health: BookHealth = BookHealth.EMPTY
    last_update_ns: int = field(default_factory=now_ns)
    messages_applied: int = 0
    rebuilds: int = 0
    cross_events: int = 0

    # -- sorted views (cached until mutation) ---------------------------------

    def _invalidate_caches(self) -> None:
        # setattr works even under __slots__ for attributes that were never set
        setattr(self, "_sorted_bids_cache", None)
        setattr(self, "_sorted_asks_cache", None)

    def sorted_bids(self) -> List[PriceLevel]:
        """Bid levels sorted price-descending (top of book first)."""
        if getattr(self, "_sorted_bids_cache", None) is None:
            self._sorted_bids_cache = sorted(
                self.bids.values(), key=lambda lvl: lvl.price, reverse=True
            )
        return self._sorted_bids_cache

    def sorted_asks(self) -> List[PriceLevel]:
        """Ask levels sorted price-ascending (top of book first)."""
        if getattr(self, "_sorted_asks_cache", None) is None:
            self._sorted_asks_cache = sorted(
                self.asks.values(), key=lambda lvl: lvl.price
            )
        return self._sorted_asks_cache

    # -- derived views ---------------------------------------------------------

    def best_bid(self) -> Optional[PriceLevel]:
        levels = self.sorted_bids()
        return levels[0] if levels else None

    def best_ask(self) -> Optional[PriceLevel]:
        levels = self.sorted_asks()
        return levels[0] if levels else None

    def total_bid_qty(self) -> int:
        return sum(lvl.quantity for lvl in self.bids.values())

    def total_ask_qty(self) -> int:
        return sum(lvl.quantity for lvl in self.asks.values())

    def top_of_book(self, ts_ns: Optional[int] = None) -> TopOfBook:
        """Compute the current :class:`TopOfBook` view."""
        ts = ts_ns if ts_ns is not None else now_ns()
        bb = self.best_bid()
        ba = self.best_ask()
        bb_px = bb.price if bb else 0.0
        bb_qty = bb.quantity if bb else 0
        ba_px = ba.price if ba else 0.0
        ba_qty = ba.quantity if ba else 0
        if bb_px > 0.0 and ba_px > 0.0:
            mid = (bb_px + ba_px) / 2.0
            spread = ba_px - bb_px
            spread_ticks = int(round(spread / self.tick_size)) if self.tick_size > 0 else 0
        else:
            mid = 0.0
            spread = 0.0
            spread_ticks = 0
        # imbalance ratio: bid_qty/ask_qty, clamped to avoid inf
        if ba_qty > 0:
            imb = bb_qty / ba_qty
        elif bb_qty > 0:
            imb = 1e6
        else:
            imb = 1.0
        return TopOfBook(
            best_bid_price=bb_px, best_bid_qty=bb_qty,
            best_ask_price=ba_px, best_ask_qty=ba_qty,
            mid_price=mid, spread=spread, spread_ticks=spread_ticks,
            imbalance_ratio=imb, timestamp_ns=ts,
        )

    def to_snapshot_dict(self, depth: int) -> Dict[str, object]:
        """Serialize the book (truncated to ``depth`` levels per side)."""
        tob = self.top_of_book()
        return {
            "sym": self.canonical_symbol,
            "ven": self.venue_id,
            "health": self.health.value,
            "tick": self.tick_size,
            "bids": [[lvl.price, lvl.quantity] for lvl in self.sorted_bids()[:depth]],
            "asks": [[lvl.price, lvl.quantity] for lvl in self.sorted_asks()[:depth]],
            "tob": tob.to_dict(),
            "msgs": self.messages_applied,
            "rebuilds": self.rebuilds,
        }
