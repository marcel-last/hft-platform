"""strategy_engine — pluggable signal strategies.

Each strategy is a stateless *evaluator* over a per-(symbol, venue)
:class:`StrategyContext`.  A strategy inspects the rolling mid-price window and
the current top-of-book view, then returns an optional :class:`SignalDecision`
describing either a new entry or the exit of a position it previously opened.

The three built-in families are:

* :class:`MomentumStrategy` — rides sustained mid-price moves in tick units.
* :class:`MeanReversionStrategy` — fades statistical deviations from the
  rolling mean (z-score based).
* :class:`SpreadArbStrategy` — trades book-structure imbalances (top-level
  bid/ask ratio) and takes profit when the spread tightens.

Strategies never touch HTTP or configuration singletons directly; all tunables
are passed in via their ``params`` dataclasses so they are trivially testable in
isolation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import (
    MeanReversionParams,
    MomentumParams,
    SpreadArbParams,
)
from .models import BookView, RollingWindow, SignalSide, now_ns

logger = logging.getLogger("ste.strategies")


@dataclass(slots=True)
class StrategyContext:
    """Per-(symbol, venue) mutable state handed to strategies each tick."""

    canonical_symbol: str
    venue_id: str
    tick_size: float
    mid_window: RollingWindow
    net_position: int = 0            # signed lots currently held by this strategy
    last_signal_ns: int = 0          # cooldown bookkeeping (set by the engine)
    open_signal_ids: List[str] = field(default_factory=list)

    def push_mid(self, price: float, timestamp_ns: int) -> None:
        self.mid_window.push(price, timestamp_ns)


@dataclass(slots=True)
class SignalDecision:
    """The outcome of one strategy evaluation for one symbol."""

    action: str                      # "ENTER" or "EXIT"
    side: SignalSide
    reason: str
    strength: float                  # normalized conviction in [0, 1]
    suggested_qty: int               # lots to trade (positive)
    reference_price: float           # price the decision fired against
    timestamp_ns: int = field(default_factory=now_ns)


class Strategy:
    """Base class for all strategies."""

    name: str = "base"

    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id
        self.state = "ACTIVE"

    # -- interface ----------------------------------------------------------

    def on_tick(self, ctx: StrategyContext, book: Optional[BookView]) -> Optional[SignalDecision]:
        """Evaluate one tick.  Returns a :class:`SignalDecision` or ``None``."""
        raise NotImplementedError

    # -- shared helpers -----------------------------------------------------

    @staticmethod
    def _clamp_strength(value: float) -> float:
        if value <= 0.0:
            return 0.0
        return min(1.0, value)


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------

class MomentumStrategy(Strategy):
    """Rides sustained mid-price moves measured in ticks over a window."""

    name = "momentum"

    def __init__(self, strategy_id: str, params: MomentumParams) -> None:
        super().__init__(strategy_id)
        self.params = params

    def on_tick(self, ctx: StrategyContext, book: Optional[BookView]) -> Optional[SignalDecision]:
        window = ctx.mid_window
        latest = window.latest()
        if latest is None or len(window) < 2:
            return None
        oldest = window.oldest()
        if oldest is None:
            return None

        tick = ctx.tick_size if ctx.tick_size > 0 else 1.0
        move_ticks = (latest.price - oldest.price) / tick

        # ---- exit an existing position on an adverse move ------------------
        if ctx.net_position != 0:
            adverse = -move_ticks if ctx.net_position > 0 else move_ticks
            if adverse >= self.params.exit_threshold_ticks:
                side = SignalSide.SELL if ctx.net_position > 0 else SignalSide.BUY
                return SignalDecision(
                    action="EXIT",
                    side=side,
                    reason=f"momentum reversal: {move_ticks:+.2f} ticks over window",
                    strength=self._clamp_strength(abs(move_ticks) / max(self.params.entry_threshold_ticks, 1e-9)),
                    suggested_qty=abs(ctx.net_position),
                    reference_price=latest.price,
                    timestamp_ns=latest.timestamp_ns,
                )

        # ---- enter on a sustained move -------------------------------------
        if abs(move_ticks) < self.params.entry_threshold_ticks:
            return None
        remaining = self.params.max_position_qty - abs(ctx.net_position)
        if remaining <= 0:
            return None
        side = SignalSide.BUY if move_ticks > 0 else SignalSide.SELL
        qty = min(remaining, max(1, int(abs(move_ticks))))
        strength = self._clamp_strength(abs(move_ticks) / (self.params.entry_threshold_ticks * 2.0))
        return SignalDecision(
            action="ENTER",
            side=side,
            reason=f"momentum: {move_ticks:+.2f} ticks over {len(window)}-sample window",
            strength=strength,
            suggested_qty=qty,
            reference_price=latest.price,
            timestamp_ns=latest.timestamp_ns,
        )


# ---------------------------------------------------------------------------
# Mean reversion
# ---------------------------------------------------------------------------

class MeanReversionStrategy(Strategy):
    """Fades statistical deviations of the mid price from its rolling mean."""

    name = "mean_reversion"

    def __init__(self, strategy_id: str, params: MeanReversionParams) -> None:
        super().__init__(strategy_id)
        self.params = params

    def on_tick(self, ctx: StrategyContext, book: Optional[BookView]) -> Optional[SignalDecision]:
        window = ctx.mid_window
        latest = window.latest()
        if latest is None or len(window) < 2:
            return None
        z = window.zscore(latest.price)

        # ---- exit when the price reverts toward the mean -------------------
        if ctx.net_position != 0:
            if ctx.net_position > 0 and z <= -self.params.z_exit_threshold:
                return SignalDecision(
                    action="EXIT", side=SignalSide.SELL,
                    reason=f"mean reversion exit: z={z:+.2f} reverted to mean",
                    strength=self._clamp_strength(abs(z) / max(self.params.z_entry_threshold, 1e-9)),
                    suggested_qty=abs(ctx.net_position),
                    reference_price=latest.price, timestamp_ns=latest.timestamp_ns,
                )
            if ctx.net_position < 0 and z >= self.params.z_exit_threshold:
                return SignalDecision(
                    action="EXIT", side=SignalSide.BUY,
                    reason=f"mean reversion exit: z={z:+.2f} reverted to mean",
                    strength=self._clamp_strength(abs(z) / max(self.params.z_entry_threshold, 1e-9)),
                    suggested_qty=abs(ctx.net_position),
                    reference_price=latest.price, timestamp_ns=latest.timestamp_ns,
                )

        # ---- enter on a statistically extreme deviation --------------------
        if abs(z) < self.params.z_entry_threshold:
            return None
        remaining = self.params.max_position_qty - abs(ctx.net_position)
        if remaining <= 0:
            return None
        side = SignalSide.BUY if z > 0 else SignalSide.SELL
        qty = min(remaining, max(1, int(abs(z))))
        strength = self._clamp_strength(abs(z) / (self.params.z_entry_threshold * 2.0))
        return SignalDecision(
            action="ENTER",
            side=side,
            reason=f"mean reversion: mid z-score {z:+.2f} vs rolling mean",
            strength=strength,
            suggested_qty=qty,
            reference_price=latest.price,
            timestamp_ns=latest.timestamp_ns,
        )


# ---------------------------------------------------------------------------
# Spread arbitrage (book-structure driven)
# ---------------------------------------------------------------------------

class SpreadArbStrategy(Strategy):
    """Trades top-level book imbalances and takes profit on spread tightening."""

    name = "spread_arb"

    def __init__(self, strategy_id: str, params: SpreadArbParams) -> None:
        super().__init__(strategy_id)
        self.params = params

    def on_tick(self, ctx: StrategyContext, book: Optional[BookView]) -> Optional[SignalDecision]:
        if book is None:
            return None
        # a usable two-sided book is required for any spread logic
        if book.best_bid_price <= 0.0 or book.best_ask_price <= 0.0:
            return None

        # ---- take profit when the spread tightens on an open position ------
        if ctx.net_position != 0 and book.spread_ticks <= self.params.exit_spread_ticks:
            side = SignalSide.SELL if ctx.net_position > 0 else SignalSide.BUY
            return SignalDecision(
                action="EXIT", side=side,
                reason=f"spread arb exit: spread tightened to {book.spread_ticks} tick(s)",
                strength=self._clamp_strength(self.params.imbalance_entry_ratio / (self.params.imbalance_entry_ratio + 1.0)),
                suggested_qty=abs(ctx.net_position),
                reference_price=book.mid_price, timestamp_ns=book.timestamp_ns,
            )

        # ---- enter on a strongly imbalanced top of book --------------------
        if book.spread_ticks > self.params.spread_max_ticks:
            return None  # too wide; no reliable liquidity to trade against
        imb = book.imbalance_ratio
        if imb >= self.params.imbalance_entry_ratio and ctx.net_position <= 0:
            remaining = self.params.max_position_qty - abs(ctx.net_position)
            if remaining > 0:
                qty = min(remaining, max(1, int(book.best_bid_qty / max(book.best_ask_qty, 1))))
                return SignalDecision(
                    action="ENTER", side=SignalSide.BUY,
                    reason=f"spread arb: bid/ask imbalance {imb:.2f} >= {self.params.imbalance_entry_ratio}",
                    strength=self._clamp_strength(imb / (self.params.imbalance_entry_ratio * 2.0)),
                    suggested_qty=qty,
                    reference_price=book.mid_price, timestamp_ns=book.timestamp_ns,
                )
        if imb > 0.0 and (1.0 / imb) >= self.params.imbalance_entry_ratio and ctx.net_position >= 0:
            remaining = self.params.max_position_qty - abs(ctx.net_position)
            if remaining > 0:
                qty = min(remaining, max(1, int(book.best_ask_qty / max(book.best_bid_qty, 1))))
                return SignalDecision(
                    action="ENTER", side=SignalSide.SELL,
                    reason=f"spread arb: ask/bid imbalance {1.0 / imb:.2f} >= {self.params.imbalance_entry_ratio}",
                    strength=self._clamp_strength((1.0 / imb) / (self.params.imbalance_entry_ratio * 2.0)),
                    suggested_qty=qty,
                    reference_price=book.mid_price, timestamp_ns=book.timestamp_ns,
                )
        return None
