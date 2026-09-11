"""strategy_engine — core domain models.

Models in this module describe the strategy engine's outputs and internal state:

* :class:`SignalSide` / :class:`SignalStatus` / :class:`StrategyState` — enums,
* :class:`Signal` — a trading signal produced by a strategy (entry/exit),
* :class:`OrderIntent` — the actionable order request pushed to the execution
  gateway (S4), derived from a signal after sizing and risk caps are applied,
* :class:`MidSample` — one mid-price observation used by the statistical
  strategies,
* :class:`BookView` — a lightweight top-of-book snapshot handed to strategies.

All timestamps are ``int64`` nanoseconds since the Unix epoch.
"""

from __future__ import annotations

import enum
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def now_ns() -> int:
    return time.time_ns()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SignalSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalStatus(str, enum.Enum):
    OPEN = "OPEN"            # live; may still trigger its exit condition
    CLOSED = "CLOSED"        # position was flattened (exit fired or manual)
    EXPIRED = "EXPIRED"      # dropped by the per-symbol open-signal cap


class StrategyState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


# ---------------------------------------------------------------------------
# Rolling statistics helpers
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MidSample:
    """One mid-price observation (price in ticks of the instrument)."""

    price: float
    timestamp_ns: int


class RollingWindow:
    """A fixed-size rolling window over :class:`MidSample` values.

    Provides O(1) amortized push and O(n) statistics recomputed on demand, which
    is fine for the small windows used by the statistical strategies (<= 64).
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("RollingWindow capacity must be >= 1")
        self._capacity = capacity
        self._items: list = []
        self._sum = 0.0

    def push(self, price: float, timestamp_ns: int) -> None:
        self._items.append(MidSample(price=price, timestamp_ns=timestamp_ns))
        self._sum += price
        if len(self._items) > self._capacity:
            dropped = self._items.pop(0)
            self._sum -= dropped.price

    def __len__(self) -> int:
        return len(self._items)

    @property
    def capacity(self) -> int:
        return self._capacity

    def latest(self) -> Optional[MidSample]:
        return self._items[-1] if self._items else None

    def oldest(self) -> Optional[MidSample]:
        return self._items[0] if self._items else None

    def mean(self) -> float:
        if not self._items:
            return 0.0
        return self._sum / len(self._items)

    def stdev(self) -> float:
        n = len(self._items)
        if n < 2:
            return 0.0
        mu = self._sum / n
        acc = 0.0
        for item in self._items:
            d = item.price - mu
            acc += d * d
        return (acc / n) ** 0.5

    def zscore(self, price: float) -> float:
        """Z-score of ``price`` against the window mean/stddev (inf-safe)."""
        sd = self.stdev()
        if sd <= 1e-9:
            return 0.0
        return (price - self.mean()) / sd

    def clear(self) -> None:
        self._items.clear()
        self._sum = 0.0


# ---------------------------------------------------------------------------
# Book view
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BookView:
    """A lightweight top-of-book snapshot handed to strategies each tick."""

    canonical_symbol: str
    venue_id: str
    best_bid_price: float
    best_bid_qty: int
    best_ask_price: float
    best_ask_qty: int
    mid_price: float
    spread: float
    spread_ticks: int
    imbalance_ratio: float
    tick_size: float
    health: str = "HEALTHY"
    timestamp_ns: int = field(default_factory=now_ns)

    @classmethod
    def from_tob_dict(cls, tob: Dict[str, Any], symbol: str, venue: str,
                      tick_size: float, health: str = "HEALTHY") -> "BookView":
        """Build a :class:`BookView` from an S2 ``tob`` dict (wire keys)."""
        bb_px = float(tob.get("bb_px", 0.0))
        ba_px = float(tob.get("ba_px", 0.0))
        mid = float(tob.get("mid", 0.0))
        spread = float(tob.get("spread", 0.0))
        return cls(
            canonical_symbol=symbol,
            venue_id=venue,
            best_bid_price=bb_px,
            best_bid_qty=int(tob.get("bb_qty", 0)),
            best_ask_price=ba_px,
            best_ask_qty=int(tob.get("ba_qty", 0)),
            mid_price=mid,
            spread=spread,
            spread_ticks=int(tob.get("spread_ticks", 0)),
            imbalance_ratio=float(tob.get("imb", 1.0)),
            tick_size=tick_size,
            health=health,
            timestamp_ns=int(tob.get("ts", now_ns())),
        )


# ---------------------------------------------------------------------------
# Signals and order intents
# ---------------------------------------------------------------------------

_signal_counter = itertools.count(1)
_intent_counter = itertools.count(1)


def _next_signal_id() -> str:
    return f"SIG-{next(_signal_counter):08d}"


def _next_intent_id() -> str:
    return f"INT-{next(_intent_counter):08d}"


@dataclass(slots=True)
class Signal:
    """A trading signal produced by a strategy for one symbol."""

    id: str
    strategy_id: str
    canonical_symbol: str
    side: SignalSide
    reason: str
    strength: float                 # normalized conviction in [0, 1]
    reference_price: float          # price at which the signal fired (mid or touch)
    suggested_qty: int              # lots the strategy wants to trade
    status: SignalStatus = SignalStatus.OPEN
    created_ns: int = field(default_factory=now_ns)
    closed_ns: Optional[int] = None

    def age_ms(self, now: Optional[int] = None) -> int:
        ts = now if now is not None else now_ns()
        return (ts - self.created_ns) // 1_000_000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "symbol": self.canonical_symbol,
            "side": self.side.value,
            "reason": self.reason,
            "strength": round(self.strength, 6),
            "ref_px": self.reference_price,
            "qty": self.suggested_qty,
            "status": self.status.value,
            "created_ns": self.created_ns,
            "closed_ns": self.closed_ns,
            "age_ms": self.age_ms(),
        }


@dataclass(slots=True)
class OrderIntent:
    """An actionable order request pushed to the execution gateway (S4)."""

    id: str
    signal_id: str
    strategy_id: str
    canonical_symbol: str
    side: SignalSide
    qty: int
    limit_price: float              # resting price for a limit order intent
    notional: float                 # qty * limit_price (informational)
    created_ns: int = field(default_factory=now_ns)
    acknowledged: bool = False      # set True once S4 accepts the intent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "symbol": self.canonical_symbol,
            "side": self.side.value,
            "qty": self.qty,
            "limit_px": self.limit_price,
            "notional": round(self.notional, 4),
            "created_ns": self.created_ns,
            "acknowledged": self.acknowledged,
        }
