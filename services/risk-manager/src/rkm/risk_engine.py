"""risk_manager — core risk engine.

The :class:`RiskEngine` is the single source of truth for pre-trade validation,
real-time exposure, breach detection, and the kill-switch.  It is thread-safe:
all mutable state is guarded by a single re-entrant lock because checks, fill
ingestion, and limit updates all mutate the same position/velocity maps.

Pre-trade check pipeline (per order intent):

    kill-switch gate -> parameter sanity -> velocity -> open-order cap
        -> per-symbol position/notional -> portfolio notional

A *SOFT* breach is a warning (order still allowed); a *HARD* breach vetoes the
order.  When ``kill_switch.auto_engage_on_hard_breach`` is set, any HARD breach
also engages the kill-switch for the whole book.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .config import ServiceConfig
from .errors import KillSwitchAlreadyEngagedError, KillSwitchNotEngagedError
from .models import (
    BreachRecord,
    BreachSeverity,
    CheckResult,
    CheckVerdict,
    KillSwitchState,
    OrderRecord,
    PreTradeRequest,
    PositionState,
    RiskLimits,
    RiskSide,
    now_ns,
)

logger = logging.getLogger("rkm.risk_engine")


class _VelocityCounter:
    """Sliding-window order counter for one symbol (or the whole book)."""

    def __init__(self, window_ms: int) -> None:
        self._window_ns = window_ms * 1_000_000
        self._events: Deque[int] = deque()

    def record(self, ts_ns: int) -> None:
        self._events.append(ts_ns)

    def count_in_window(self, now: int) -> int:
        cutoff = now - self._window_ns
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()
        return len(self._events)


class RiskEngine:
    """Owns positions, open orders, velocity counters, limits, breaches, kill-switch."""

    def __init__(self, cfg: ServiceConfig) -> None:
        self.cfg = cfg
        self.limits = RiskLimits(
            max_position_qty=cfg.limits.max_position_qty,
            max_notional_per_symbol=cfg.limits.max_notional_per_symbol,
            max_portfolio_notional=cfg.limits.max_portfolio_notional,
            max_order_qty=cfg.limits.max_order_qty,
            max_order_notional=cfg.limits.max_order_notional,
            min_price=cfg.limits.min_price,
        )
        self._lock = threading.RLock()
        self._positions: Dict[str, PositionState] = {}
        self._open_orders: Dict[str, OrderRecord] = {}
        self._velocity_symbol: Dict[str, _VelocityCounter] = {}
        self._velocity_total = _VelocityCounter(cfg.velocity.window_ms)
        self._breaches: Deque[BreachRecord] = deque()
        self._max_breaches = 4096
        self._ref_prices: Dict[str, float] = {}
        self.kill_switch_state = KillSwitchState.DISARMED
        self.kill_switch_reason = ""
        self.kill_switch_engaged_ns: Optional[int] = None
        # statistics counters (monotonic)
        self.stats: Dict[str, int] = {
            "pre_trade_checks": 0,
            "allowed": 0,
            "vetoed": 0,
            "soft_breaches": 0,
            "hard_breaches": 0,
            "fills_applied": 0,
            "kill_switch_engagements": 0,
        }

    # ------------------------------------------------------------------
    # Position / order maintenance
    # ------------------------------------------------------------------

    def position(self, symbol: str) -> PositionState:
        with self._lock:
            return self._positions.get(symbol) or self._ensure_position_locked(symbol)

    def _ensure_position_locked(self, symbol: str) -> PositionState:
        pos = self._positions.get(symbol)
        if pos is None:
            pos = PositionState(canonical_symbol=symbol)
            self._positions[symbol] = pos
        return pos

    def apply_fill(self, symbol: str, side: RiskSide, qty: int, price: float,
                   ts_ns: Optional[int] = None) -> PositionState:
        """Apply one venue fill to the authoritative position state."""
        ts = ts_ns if ts_ns is not None else now_ns()
        with self._lock:
            pos = self._ensure_position_locked(symbol)
            pos.apply_fill(side, qty, price, ts)
            self.stats["fills_applied"] += 1
            return pos

    def set_open_orders(self, orders: List[OrderRecord]) -> None:
        """Replace the snapshot of open orders (from S4 GET /orders)."""
        with self._lock:
            self._open_orders = {o.id: o for o in orders}

    def add_open_order(self, order: OrderRecord) -> None:
        with self._lock:
            self._open_orders[order.id] = order

    def remove_open_order(self, order_id: str) -> None:
        with self._lock:
            self._open_orders.pop(order_id, None)

    def open_orders(self) -> List[OrderRecord]:
        with self._lock:
            return list(self._open_orders.values())

    def set_reference_price(self, symbol: str, price: float) -> None:
        """Record the latest reference (mid) price for a symbol (from S2 ToB)."""
        if price > 0:
            with self._lock:
                self._ref_prices[symbol] = price

    def _effective_price_locked(self, request: PreTradeRequest) -> float:
        """Price used for notional math: the order's limit price, else last ref."""
        if request.limit_price > 0:
            return request.limit_price
        return self._ref_prices.get(request.canonical_symbol, 0.0)

    # ------------------------------------------------------------------
    # Limits management
    # ------------------------------------------------------------------

    def update_limits(self, body: Dict[str, Any]) -> RiskLimits:
        """Apply a partial limit update (PUT /limits).  Unknown keys are ignored."""
        with self._lock:
            scalar_keys = {
                "max_position_qty": int,
                "max_notional_per_symbol": float,
                "max_portfolio_notional": float,
                "max_order_qty": int,
                "max_order_notional": float,
                "min_price": float,
            }
            for key, cast in scalar_keys.items():
                if key in body:
                    try:
                        setattr(self.limits, key, cast(body[key]))
                    except (TypeError, ValueError):
                        logger.warning("ignoring non-numeric limit %s=%r", key, body.get(key))
            overrides = body.get("symbol_overrides")
            if isinstance(overrides, dict):
                for symbol, ov in overrides.items():
                    if not isinstance(ov, dict):
                        continue
                    clean: Dict[str, float] = {}
                    if "max_position_qty" in ov:
                        try:
                            clean["max_position_qty"] = int(ov["max_position_qty"])
                        except (TypeError, ValueError):
                            pass
                    if "max_notional_per_symbol" in ov:
                        try:
                            clean["max_notional_per_symbol"] = float(ov["max_notional_per_symbol"])
                        except (TypeError, ValueError):
                            pass
                    if clean:
                        self.limits.symbol_overrides[str(symbol)] = clean
            return self.limits

    def reset_limits(self) -> RiskLimits:
        """Restore limits to the configured defaults."""
        with self._lock:
            cfg = self.cfg.limits
            self.limits.max_position_qty = cfg.max_position_qty
            self.limits.max_notional_per_symbol = cfg.max_notional_per_symbol
            self.limits.max_portfolio_notional = cfg.max_portfolio_notional
            self.limits.max_order_qty = cfg.max_order_qty
            self.limits.max_order_notional = cfg.max_order_notional
            self.limits.min_price = cfg.min_price
            self.limits.symbol_overrides.clear()
            return self.limits

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def pre_trade_check(self, request: PreTradeRequest) -> CheckResult:
        """Run the full pre-trade pipeline for one order intent."""
        with self._lock:
            now = now_ns()
            verdicts: List[CheckVerdict] = []
            breached = False
            hard_breached = False

            def _record(v: CheckVerdict) -> None:
                nonlocal breached, hard_breached
                verdicts.append(v)
                if not v.passed:
                    breached = True
                    if v.severity is BreachSeverity.HARD:
                        hard_breached = True
                    self._append_breach_locked(request.canonical_symbol, request, v)

            # 1. Kill-switch gate: while engaged, no new orders are allowed at all.
            if self.kill_switch_state is KillSwitchState.ENGAGED:
                _record(CheckVerdict(
                    name="kill_switch", passed=False, severity=BreachSeverity.HARD,
                    detail=f"kill-switch engaged ({self.kill_switch_reason or 'no reason recorded'})",
                    context={"state": self.kill_switch_state.value},
                ))
                self.stats["pre_trade_checks"] += 1
                self.stats["vetoed"] += 1
                return CheckResult(allowed=False, verdicts=verdicts, breached=True, hard_breached=True)

            # 2. Parameter sanity (malformed orders are a client error, not a breach).
            price = self._effective_price_locked(request)
            if request.qty == 0:
                _record(CheckVerdict("order_size", False, BreachSeverity.HARD,
                                     "order quantity is zero"))
                verdicts.append(CheckVerdict("price_sanity", True, BreachSeverity.HARD))
            elif price <= self.limits.min_price:
                _record(CheckVerdict(
                    "price_sanity", False, BreachSeverity.HARD,
                    f"limit price {request.limit_price} at/below minimum {self.limits.min_price}",
                    context={"limit_px": request.limit_price, "min_price": self.limits.min_price},
                ))
                verdicts.append(CheckVerdict("order_size", True, BreachSeverity.HARD))
            else:
                verdicts.append(CheckVerdict("order_size", True, BreachSeverity.HARD))
                verdicts.append(CheckVerdict("price_sanity", True, BreachSeverity.HARD))

            # 3. Single-order size caps (HARD).
            if abs(request.qty) > self.limits.max_order_qty:
                _record(CheckVerdict(
                    "max_order_qty", False, BreachSeverity.HARD,
                    f"order qty {abs(request.qty)} exceeds max_order_qty {self.limits.max_order_qty}",
                    context={"qty": abs(request.qty), "limit": self.limits.max_order_qty},
                ))
            else:
                verdicts.append(CheckVerdict("max_order_qty", True, BreachSeverity.HARD))

            order_notional = request.notional if request.limit_price > 0 else abs(request.qty) * price
            if order_notional > self.limits.max_order_notional:
                _record(CheckVerdict(
                    "max_order_notional", False, BreachSeverity.HARD,
                    f"order notional {order_notional:.2f} exceeds max_order_notional "
                    f"{self.limits.max_order_notional:.2f}",
                    context={"notional": round(order_notional, 4),
                             "limit": self.limits.max_order_notional},
                ))
            else:
                verdicts.append(CheckVerdict("max_order_notional", True, BreachSeverity.HARD))

            # 4. Velocity limits (HARD): per-symbol and portfolio-wide sliding windows.
            sym_counter = self._velocity_symbol.setdefault(
                request.canonical_symbol, _VelocityCounter(self.cfg.velocity.window_ms))
            sym_count = sym_counter.count_in_window(now) + 1
            if sym_count > self.cfg.velocity.max_orders_per_symbol:
                _record(CheckVerdict(
                    "velocity_symbol", False, BreachSeverity.HARD,
                    f"{sym_count} orders for {request.canonical_symbol} in the last "
                    f"{self.cfg.velocity.window_ms}ms exceeds max {self.cfg.velocity.max_orders_per_symbol}",
                    context={"count": sym_count, "limit": self.cfg.velocity.max_orders_per_symbol},
                ))
            else:
                verdicts.append(CheckVerdict("velocity_symbol", True, BreachSeverity.HARD))

            total_count = self._velocity_total.count_in_window(now) + 1
            if total_count > self.cfg.velocity.max_orders_total:
                _record(CheckVerdict(
                    "velocity_total", False, BreachSeverity.HARD,
                    f"{total_count} orders in the last {self.cfg.velocity.window_ms}ms exceeds "
                    f"max {self.cfg.velocity.max_orders_total}",
                    context={"count": total_count, "limit": self.cfg.velocity.max_orders_total},
                ))
            else:
                verdicts.append(CheckVerdict("velocity_total", True, BreachSeverity.HARD))

            # 5. Open-order cap (HARD): portfolio-wide count of concurrently open orders.
            open_count = sum(1 for o in self._open_orders.values() if o.is_open) + 1
            if open_count > self.cfg.kill_switch.max_open_orders:
                _record(CheckVerdict(
                    "max_open_orders", False, BreachSeverity.HARD,
                    f"{open_count} open orders exceeds max_open_orders {self.cfg.kill_switch.max_open_orders}",
                    context={"count": open_count, "limit": self.cfg.kill_switch.max_open_orders},
                ))
            else:
                verdicts.append(CheckVerdict("max_open_orders", True, BreachSeverity.HARD))

            # 6. Per-symbol position limit (HARD) — projected net after this order.
            pos = self._ensure_position_locked(request.canonical_symbol)
            projected_net = pos.net_qty + request.side.sign * abs(request.qty)
            pos_limit = self.limits.position_limit(request.canonical_symbol)
            if abs(projected_net) > pos_limit:
                _record(CheckVerdict(
                    "position_limit", False, BreachSeverity.HARD,
                    f"projected net {projected_net} exceeds position limit +/-{pos_limit} "
                    f"for {request.canonical_symbol}",
                    context={"current": pos.net_qty, "projected": projected_net,
                             "limit": pos_limit},
                ))
            else:
                verdicts.append(CheckVerdict(
                    "position_limit", True, BreachSeverity.HARD,
                    context={"current": pos.net_qty, "projected": projected_net, "limit": pos_limit}))

            # 7. Per-symbol notional limit (SOFT at >=80%, HARD when exceeded).
            ref = self._effective_price_locked(request)
            projected_notional = abs(projected_net) * ref if ref > 0 else order_notional
            sym_notional_limit = self.limits.notional_limit(request.canonical_symbol)
            if projected_notional > sym_notional_limit:
                _record(CheckVerdict(
                    "notional_limit", False, BreachSeverity.HARD,
                    f"projected notional {projected_notional:.2f} exceeds per-symbol limit "
                    f"{sym_notional_limit:.2f}",
                    context={"projected": round(projected_notional, 4),
                             "limit": sym_notional_limit},
                ))
            elif projected_notional >= 0.8 * sym_notional_limit:
                _record(CheckVerdict(
                    "notional_limit", False, BreachSeverity.SOFT,
                    f"projected notional {projected_notional:.2f} is within 20% of the per-symbol "
                    f"limit {sym_notional_limit:.2f}",
                    context={"projected": round(projected_notional, 4),
                             "limit": sym_notional_limit},
                ))
            else:
                verdicts.append(CheckVerdict(
                    "notional_limit", True, BreachSeverity.HARD,
                    context={"projected": round(projected_notional, 4),
                             "limit": sym_notional_limit}))

            # 8. Portfolio gross notional (SOFT at >=80%, HARD when exceeded).
            portfolio = self._portfolio_gross_notional_locked() + order_notional
            if portfolio > self.limits.max_portfolio_notional:
                _record(CheckVerdict(
                    "portfolio_notional", False, BreachSeverity.HARD,
                    f"projected portfolio notional {portfolio:.2f} exceeds limit "
                    f"{self.limits.max_portfolio_notional:.2f}",
                    context={"projected": round(portfolio, 4),
                             "limit": self.limits.max_portfolio_notional},
                ))
            elif portfolio >= 0.8 * self.limits.max_portfolio_notional:
                _record(CheckVerdict(
                    "portfolio_notional", False, BreachSeverity.SOFT,
                    f"projected portfolio notional {portfolio:.2f} is within 20% of the limit "
                    f"{self.limits.max_portfolio_notional:.2f}",
                    context={"projected": round(portfolio, 4),
                             "limit": self.limits.max_portfolio_notional},
                ))
            else:
                verdicts.append(CheckVerdict(
                    "portfolio_notional", True, BreachSeverity.HARD,
                    context={"projected": round(portfolio, 4),
                             "limit": self.limits.max_portfolio_notional}))

            allowed = not hard_breached
            if allowed:
                # Only commit velocity counters for orders that are actually allowed.
                sym_counter.record(now)
                self._velocity_total.record(now)
                self.stats["allowed"] += 1
            else:
                self.stats["vetoed"] += 1

            if breached:
                self.stats["soft_breaches"] += sum(
                    1 for v in verdicts if not v.passed and v.severity is BreachSeverity.SOFT)
                self.stats["hard_breaches"] += sum(
                    1 for v in verdicts if not v.passed and v.severity is BreachSeverity.HARD)

            # Auto kill-switch on hard breach (configurable).
            if hard_breached and self.cfg.kill_switch.auto_engage_on_hard_breach:
                self._engage_locked(
                    "auto-engaged after hard risk breach",
                    context={"request_id": request.id, "symbol": request.canonical_symbol},
                )

            self.stats["pre_trade_checks"] += 1
            return CheckResult(allowed=allowed, verdicts=verdicts,
                               breached=breached, hard_breached=hard_breached)

    def _portfolio_gross_notional_locked(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            if pos.net_qty == 0:
                continue
            ref = self._ref_prices.get(pos.canonical_symbol, pos.avg_price)
            total += abs(pos.net_qty) * ref if ref > 0 else pos.gross_notional
        return total

    def _append_breach_locked(self, symbol: str, request: PreTradeRequest,
                              verdict: CheckVerdict) -> BreachRecord:
        record = BreachRecord.create(
            symbol=symbol,
            check_name=verdict.name,
            severity=verdict.severity,
            message=f"{verdict.name}: {verdict.detail}",
            context={"request_id": request.id, **verdict.context},
        )
        self._breaches.append(record)
        while len(self._breaches) > self._max_breaches:
            self._breaches.popleft()
        return record

    # ------------------------------------------------------------------
    # Breaches / exposure / stats views
    # ------------------------------------------------------------------

    def breaches(self, limit: int = 100, severity: Optional[str] = None) -> List[BreachRecord]:
        with self._lock:
            items = list(self._breaches)
        if severity:
            sev = severity.upper()
            if sev in ("SOFT", "HARD"):
                items = [b for b in items if b.severity.value == sev]
        return items[-limit:]

    def mark_breach_escalated(self, breach_id: str) -> bool:
        with self._lock:
            for record in self._breaches:
                if record.id == breach_id:
                    record.escalated = True
                    return True
        return False

    def exposure(self) -> Dict[str, Any]:
        """Current portfolio exposure snapshot (positions + open orders + totals)."""
        with self._lock:
            positions = []
            gross_total = 0.0
            for symbol in sorted(self._positions):
                pos = self._positions[symbol]
                ref = self._ref_prices.get(symbol, pos.avg_price)
                market_value = pos.net_qty * ref if ref > 0 else 0.0
                row = pos.to_dict(ref_price=ref if ref > 0 else None)
                positions.append(row)
                gross_total += abs(pos.net_qty) * ref if ref > 0 else pos.gross_notional
            open_orders = [o for o in self._open_orders.values() if o.is_open]
            pending_notional = sum(o.notional for o in open_orders)
            return {
                "positions": positions,
                "gross_notional": round(gross_total, 4),
                "portfolio_notional_limit": self.limits.max_portfolio_notional,
                "open_orders": len(open_orders),
                "pending_notional": round(pending_notional, 4),
                "kill_switch": self.kill_switch_state.value,
            }

    def stats_view(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.stats)

    # ------------------------------------------------------------------
    # Kill-switch
    # ------------------------------------------------------------------

    def engage_kill_switch(self, reason: str = "manual") -> Dict[str, Any]:
        """Engage the kill-switch; returns a summary of the state transition."""
        with self._lock:
            if self.kill_switch_state is KillSwitchState.ENGAGED:
                raise KillSwitchAlreadyEngagedError()
            self._engage_locked(reason or "manual", context={})

    def _engage_locked(self, reason: str, context: Dict[str, Any]) -> None:
        self.kill_switch_state = KillSwitchState.ENGAGED
        self.kill_switch_reason = reason
        self.kill_switch_engaged_ns = now_ns()
        self.stats["kill_switch_engagements"] += 1
        logger.warning("KILL-SWITCH ENGAGED: %s (%s)", reason, context)

    def disengage_kill_switch(self) -> Dict[str, Any]:
        """Disarm the kill-switch (manual re-enable)."""
        with self._lock:
            if self.kill_switch_state is KillSwitchState.DISARMED:
                raise KillSwitchNotEngagedError()
            self.kill_switch_state = KillSwitchState.DISARMED
            self.kill_switch_reason = ""
            self.kill_switch_engaged_ns = None
            logger.info("kill-switch disarmed")

    def kill_switch_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.kill_switch_state.value,
                "reason": self.kill_switch_reason,
                "engaged_ns": self.kill_switch_engaged_ns,
                "flatten_on_engage": self.cfg.kill_switch.flatten_on_engage,
                "auto_engage_on_hard_breach": self.cfg.kill_switch.auto_engage_on_hard_breach,
            }
