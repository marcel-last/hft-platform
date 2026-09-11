"""strategy_engine — signal engine (core orchestration).

The :class:`SignalEngine` owns one :class:`StrategyContext` per
(canonical_symbol, venue) and drives every registered strategy against it.  On
each tick it:

1.  Pushes the current mid price into the symbol's rolling window.
2.  Runs each active strategy for that symbol in registration order.
3.  Applies cooldowns, open-signal caps, and risk limits to any decision.
4.  Records the resulting :class:`Signal` and (for entries) builds an
    :class:`OrderIntent` sized against the configured caps.

The engine is deliberately transport-agnostic: it never performs HTTP itself.
Clients are injected by the runtime, which calls :meth:`apply_book_view` /
:meth:`apply_quote` from its ingest loop and then pushes any new intents to the
execution gateway.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from .config import CONFIG, ServiceConfig
from .errors import RiskLimitExceededError
from .models import (
    BookView,
    OrderIntent,
    RollingWindow,
    Signal,
    SignalSide,
    SignalStatus,
    StrategyState,
    now_ns,
)
from .strategies import (
    MeanReversionStrategy,
    MomentumStrategy,
    SpreadArbStrategy,
    Strategy,
    StrategyContext,
    SignalDecision,
)

logger = logging.getLogger("ste.signal_engine")


class SignalEngine:
    """Owns all strategy contexts and turns ticks into signals/intents."""

    def __init__(self, cfg: Optional[ServiceConfig] = None) -> None:
        self.cfg = cfg or CONFIG
        # (symbol, venue) -> StrategyContext
        self._contexts: Dict[Tuple[str, str], StrategyContext] = {}
        # strategy_id -> Strategy instance
        self._strategies: Dict[str, Strategy] = {}
        # signal id -> Signal (rolling buffer)
        self._signals: Dict[str, Signal] = {}
        # intent id -> OrderIntent (rolling buffer)
        self._intents: Dict[str, OrderIntent] = {}
        self._max_signals = 4096
        self._tick_sizes: Dict[str, float] = {}

        self.stats = {
            "ticks_processed": 0,
            "decisions_evaluated": 0,
            "signals_generated": 0,
            "intents_emitted": 0,
            "cooldown_skips": 0,
            "risk_rejects": 0,
        }

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    def register_strategy(self, strategy: Strategy) -> None:
        """Register a strategy instance by id (idempotent)."""
        self._strategies[strategy.strategy_id] = strategy
        logger.info("registered strategy %s (%s)", strategy.strategy_id, strategy.name)

    def default_strategies(self) -> List[Strategy]:
        """Build the three built-in strategies from configuration."""
        return [
            MomentumStrategy("mom-default", self.cfg.strategy.momentum),
            MeanReversionStrategy("mr-default", self.cfg.strategy.mean_reversion),
            SpreadArbStrategy("arb-default", self.cfg.strategy.spread_arb),
        ]

    def register_default_strategies(self) -> None:
        for strategy in self.default_strategies():
            self.register_strategy(strategy)

    def set_tick_size(self, canonical_symbol: str, tick_size: float) -> None:
        if tick_size > 0:
            self._tick_sizes[canonical_symbol] = tick_size

    def _tick_for(self, canonical_symbol: str) -> float:
        return self._tick_sizes.get(canonical_symbol, 0.25)

    # -- contexts ---------------------------------------------------------

    def ensure_context(self, canonical_symbol: str, venue_id: str) -> StrategyContext:
        key = (canonical_symbol, venue_id)
        ctx = self._contexts.get(key)
        if ctx is None:
            window = RollingWindow(max(self.cfg.strategy.momentum.window_ticks,
                                       self.cfg.strategy.mean_reversion.window_ticks))
            ctx = StrategyContext(
                canonical_symbol=canonical_symbol,
                venue_id=venue_id,
                tick_size=self._tick_for(canonical_symbol),
                mid_window=window,
            )
            self._contexts[key] = ctx
        return ctx

    def get_context(self, canonical_symbol: str, venue_id: str) -> Optional[StrategyContext]:
        return self._contexts.get((canonical_symbol, venue_id))

    def all_contexts(self) -> List[StrategyContext]:
        return list(self._contexts.values())

    # ------------------------------------------------------------------
    # Tick application (hot path)
    # ------------------------------------------------------------------

    def apply_book_view(self, book: BookView) -> Tuple[List[Signal], List[OrderIntent]]:
        """Process one top-of-book view for a symbol/venue.

        Returns ``(new_signals, new_intents)`` produced this tick.
        """
        if book.mid_price <= 0.0:
            return [], []
        ctx = self.ensure_context(book.canonical_symbol, book.venue_id)
        ctx.tick_size = book.tick_size if book.tick_size > 0 else ctx.tick_size
        ctx.push_mid(book.mid_price, book.timestamp_ns)
        self.stats["ticks_processed"] += 1
        return self._evaluate(ctx, book)

    def apply_quote(self, quote_wire: dict) -> Tuple[List[Signal], List[OrderIntent]]:
        """Process one raw normalized quote (S1 wire format) for a symbol.

        Used when no book view is available yet: the quote's price seeds the
        mid window so the statistical strategies can warm up.  Strategies that
        require a two-sided book simply return ``None`` until a real book view
        arrives.
        """
        symbol = quote_wire.get("sym") or quote_wire.get("v")
        if not symbol:
            return [], []
        venue = quote_wire.get("ven", "primary")
        price = float(quote_wire.get("px", 0.0))
        if price <= 0.0:
            return [], []
        ts = int(quote_wire.get("rt", now_ns()))
        ctx = self.ensure_context(symbol, venue)
        ctx.push_mid(price, ts)
        self.stats["ticks_processed"] += 1
        book = BookView(
            canonical_symbol=symbol, venue_id=venue,
            best_bid_price=0.0, best_bid_qty=0,
            best_ask_price=0.0, best_ask_qty=0,
            mid_price=price, spread=0.0, spread_ticks=0,
            imbalance_ratio=1.0, tick_size=self._tick_for(symbol),
            health="EMPTY", timestamp_ns=ts,
        )
        return self._evaluate(ctx, book)

    def _evaluate(self, ctx: StrategyContext, book: BookView) -> Tuple[List[Signal], List[OrderIntent]]:
        """Run every active strategy against one context and record outcomes."""
        new_signals: List[Signal] = []
        new_intents: List[OrderIntent] = []
        now = now_ns()

        for strategy in self._strategies.values():
            if getattr(strategy, "state", "ACTIVE") == StrategyState.PAUSED.value:
                continue
            decision = strategy.on_tick(ctx, book)
            if decision is None:
                continue
            self.stats["decisions_evaluated"] += 1
            outcome = self._apply_decision(strategy, ctx, decision, now)
            if outcome is not None:
                sig, intent = outcome
                new_signals.append(sig)
                if intent is not None:
                    new_intents.append(intent)
        return new_signals, new_intents

    def _apply_decision(self, strategy: Strategy, ctx: StrategyContext,
                        decision: SignalDecision, now: int) -> Optional[Tuple[Signal, Optional[OrderIntent]]]:
        """Turn one decision into a recorded signal (+intent), enforcing limits."""
        # cooldown gate (per strategy+symbol)
        if now - ctx.last_signal_ns < self.cfg.signal.cooldown_ms * 1_000_000:
            self.stats["cooldown_skips"] += 1
            return None

        qty = max(1, min(int(decision.suggested_qty), self.cfg.signal.max_order_qty))

        if decision.action == "EXIT":
            # flatten the position this strategy holds for the symbol
            ctx.net_position = 0
            ctx.open_signal_ids.clear()
            signal = self._record_signal(strategy.strategy_id, ctx.canonical_symbol,
                                         decision.side, decision.reason, decision.strength,
                                         qty, decision.reference_price, now)
            ctx.last_signal_ns = now
            return (signal, None)

        # ENTER: enforce per-symbol open-signal cap before recording a new one
        self._enforce_open_cap(ctx, now)
        if abs(ctx.net_position) >= self.cfg.signal.max_open_signals_per_symbol * self.cfg.signal.max_order_qty:
            self.stats["cooldown_skips"] += 1
            return None

        limit_price = decision.reference_price
        notional = qty * limit_price
        if notional > self.cfg.emit.max_notional_per_signal:
            # scale quantity down to fit the notional cap (floor at 1 lot)
            scaled = max(1, int(self.cfg.emit.max_notional_per_signal / max(limit_price, 1e-9)))
            qty = min(qty, scaled)
            notional = qty * limit_price

        signal = self._record_signal(strategy.strategy_id, ctx.canonical_symbol,
                                     decision.side, decision.reason, decision.strength,
                                     qty, decision.reference_price, now)

        # update the strategy's signed position for this symbol
        delta = qty if decision.side == SignalSide.BUY else -qty
        ctx.net_position += delta
        ctx.open_signal_ids.append(signal.id)
        ctx.last_signal_ns = now

        intent = self._build_intent(signal, strategy.strategy_id, decision.side,
                                    qty, limit_price, notional, now)
        return (signal, intent)

    # ------------------------------------------------------------------
    # Signal / intent bookkeeping
    # ------------------------------------------------------------------

    def _enforce_open_cap(self, ctx: StrategyContext, now: int) -> None:
        """Expire the oldest open signals beyond the per-symbol cap."""
        cap = self.cfg.signal.max_open_signals_per_symbol
        while len(ctx.open_signal_ids) >= cap:
            oldest_id = ctx.open_signal_ids.pop(0)
            sig = self._signals.get(oldest_id)
            if sig is not None and sig.status == SignalStatus.OPEN:
                sig.status = SignalStatus.EXPIRED
                sig.closed_ns = now

    def _record_signal(self, strategy_id: str, symbol: str, side: SignalSide,
                       reason: str, strength: float, qty: int, ref_px: float,
                       now: int) -> Signal:
        signal = Signal(
            id=f"SIG-{self.stats['signals_generated'] + 1:08d}",
            strategy_id=strategy_id,
            canonical_symbol=symbol,
            side=side,
            reason=reason,
            strength=strength,
            reference_price=ref_px,
            suggested_qty=qty,
            status=SignalStatus.OPEN,
            created_ns=now,
        )
        self.stats["signals_generated"] += 1
        self._signals[signal.id] = signal
        while len(self._signals) > self._max_signals:
            oldest_key = next(iter(self._signals))
            del self._signals[oldest_key]
        return signal

    def _build_intent(self, signal: Signal, strategy_id: str, side: SignalSide,
                      qty: int, limit_price: float, notional: float, now: int) -> OrderIntent:
        intent = OrderIntent(
            id=f"INT-{self.stats['intents_emitted'] + 1:08d}",
            signal_id=signal.id,
            strategy_id=strategy_id,
            canonical_symbol=signal.canonical_symbol,
            side=side,
            qty=qty,
            limit_price=limit_price,
            notional=notional,
            created_ns=now,
        )
        self.stats["intents_emitted"] += 1
        self._intents[intent.id] = intent
        while len(self._intents) > self.cfg.emit.max_open_intents:
            oldest_key = next(iter(self._intents))
            del self._intents[oldest_key]
        return intent

    # ------------------------------------------------------------------
    # Query / control surface (used by the controller)
    # ------------------------------------------------------------------

    def signals(self, symbol: Optional[str] = None, limit: int = 100) -> List[Signal]:
        items = list(self._signals.values())
        if symbol is not None:
            items = [s for s in items if s.canonical_symbol == symbol]
        items.sort(key=lambda s: s.created_ns, reverse=True)
        return items[:limit]

    def intents(self, limit: int = 100) -> List[OrderIntent]:
        items = list(self._intents.values())
        items.sort(key=lambda i: i.created_ns, reverse=True)
        return items[:limit]

    def get_signal(self, signal_id: str) -> Optional[Signal]:
        return self._signals.get(signal_id)

    def mark_intent_acknowledged(self, intent_id: str) -> bool:
        intent = self._intents.get(intent_id)
        if intent is None:
            return False
        intent.acknowledged = True
        return True

    def close_signal(self, signal_id: str) -> Optional[Signal]:
        """Manually close a live signal and flatten the strategy position."""
        sig = self._signals.get(signal_id)
        if sig is None or sig.status != SignalStatus.OPEN:
            return sig
        now = now_ns()
        sig.status = SignalStatus.CLOSED
        sig.closed_ns = now
        ctx = self.get_context(sig.canonical_symbol, "primary")
        # flatten across every venue context for this symbol (positions are
        # tracked per venue; a manual close flattens all of them)
        for c in self.all_contexts():
            if c.canonical_symbol == sig.canonical_symbol:
                c.net_position = 0
                c.open_signal_ids = [i for i in c.open_signal_ids if i != signal_id]
        return sig

    def pause_strategy(self, strategy_id: str) -> bool:
        strategy = self._strategies.get(strategy_id)
        if strategy is None:
            return False
        strategy.state = StrategyState.PAUSED.value
        logger.info("strategy %s paused", strategy_id)
        return True

    def resume_strategy(self, strategy_id: str) -> bool:
        strategy = self._strategies.get(strategy_id)
        if strategy is None:
            return False
        strategy.state = StrategyState.ACTIVE.value
        logger.info("strategy %s resumed", strategy_id)
        return True

    def strategy_states(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for sid, strategy in self._strategies.items():
            state = getattr(strategy, "state", StrategyState.ACTIVE.value)
            out[sid] = state.value if isinstance(state, StrategyState) else state
        return out
