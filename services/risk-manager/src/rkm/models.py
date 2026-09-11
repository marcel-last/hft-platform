"""risk_manager — core domain models.

Models in this module describe the risk manager's state and I/O:

* :class:`RiskSide` / :class:`BreachSeverity` / :class:`KillSwitchState` — enums,
* :class:`PositionState` — net position + average entry price for one symbol,
* :class:`OrderRecord` — a lightweight view of an open order (from S4),
* :class:`RiskLimits` — the mutable, overridable set of risk limits,
* :class:`PreTradeRequest` / :class:`CheckVerdict` / :class:`CheckResult` —
  the pre-trade check request and its verdict,
* :class:`BreachRecord` — a recorded limit breach (SOFT or HARD).

All timestamps are ``int64`` nanoseconds since the Unix epoch.
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

class RiskSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """Signed lot multiplier used in position math."""
        return 1 if self is RiskSide.BUY else -1


class BreachSeverity(str, enum.Enum):
    SOFT = "SOFT"          # approaching a limit; warning only, order still allowed
    HARD = "HARD"          # a hard limit would be exceeded; order must be vetoed


class KillSwitchState(str, enum.Enum):
    DISARMED = "DISARMED"
    ENGAGED = "ENGAGED"


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PositionState:
    """Net position and average entry price for one canonical symbol."""

    canonical_symbol: str
    net_qty: int = 0                    # signed lots (long positive, short negative)
    avg_price: float = 0.0              # volume-weighted average entry price
    gross_notional: float = 0.0         # |net_qty| * avg_price (informational)
    updated_ns: int = field(default_factory=now_ns)

    def apply_fill(self, side: RiskSide, qty: int, price: float, ts_ns: int) -> None:
        """Apply one fill to the position using standard average-cost accounting.

        Buying into a short (or selling into a long) reduces |net| and lowers the
        gross notional proportionally; adding to a position re-averages the entry
        price.  A full flip is handled by closing the old side then opening the new.
        """
        if qty == 0:
            self.updated_ns = ts_ns
            return
        signed = side.sign * int(qty)
        prev_net = self.net_qty
        new_net = prev_net + signed

        if prev_net == 0 or (prev_net > 0) == (signed > 0):
            # Opening fresh or adding to the same direction: re-average entry price.
            total_cost = abs(prev_net) * self.avg_price + abs(signed) * price
            self.avg_price = (total_cost / abs(new_net)) if new_net != 0 else 0.0
        else:
            # Reducing or flipping the position.
            closing = min(abs(prev_net), abs(signed))
            remaining_signed = signed - (1 if prev_net > 0 else -1) * closing
            if remaining_signed == 0:
                # Fully closed: no open position, reset entry price.
                self.avg_price = 0.0
            elif abs(remaining_signed) < abs(prev_net):
                # Pure reduction (same direction as before): keep the original
                # average entry cost of the remaining lots unchanged.
                pass
            else:
                # Flipped to the opposite side: rebase entry at this fill price.
                self.avg_price = price

        self.net_qty = new_net
        self.gross_notional = abs(new_net) * self.avg_price
        self.updated_ns = ts_ns

    def to_dict(self, ref_price: Optional[float] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "symbol": self.canonical_symbol,
            "net_qty": self.net_qty,
            "avg_price": round(self.avg_price, 6),
            "gross_notional": round(self.gross_notional, 4),
            "updated_ns": self.updated_ns,
        }
        if ref_price is not None:
            body["ref_px"] = ref_price
            body["market_value"] = round(self.net_qty * ref_price, 4)
            body["unrealized_pnl"] = round(self.net_qty * (ref_price - self.avg_price), 4)
        return body


# ---------------------------------------------------------------------------
# Open orders
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class OrderRecord:
    """A lightweight view of an order currently live at the venue boundary."""

    id: str
    canonical_symbol: str
    side: RiskSide
    qty: int
    limit_price: float
    state: str
    updated_ns: int = field(default_factory=now_ns)

    @property
    def is_open(self) -> bool:
        return self.state in ("NEW", "PARTIALLY_FILLED")

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.limit_price


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

@dataclass
class RiskLimits:
    """The mutable set of risk limits, overridable at runtime via PUT /limits.

    ``max_position_qty`` and ``max_notional_per_symbol`` may be overridden per
    symbol in :attr:`symbol_overrides`; everything else is portfolio-wide.
    """

    max_position_qty: int = 100
    max_notional_per_symbol: float = 5_000_000.0
    max_portfolio_notional: float = 25_000_000.0
    max_order_qty: int = 100
    max_order_notional: float = 1_000_000.0
    min_price: float = 0.0
    symbol_overrides: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def position_limit(self, symbol: str) -> int:
        ov = self.symbol_overrides.get(symbol, {})
        return int(ov.get("max_position_qty", self.max_position_qty))

    def notional_limit(self, symbol: str) -> float:
        ov = self.symbol_overrides.get(symbol, {})
        return float(ov.get("max_notional_per_symbol", self.max_notional_per_symbol))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_position_qty": self.max_position_qty,
            "max_notional_per_symbol": self.max_notional_per_symbol,
            "max_portfolio_notional": self.max_portfolio_notional,
            "max_order_qty": self.max_order_qty,
            "max_order_notional": self.max_order_notional,
            "min_price": self.min_price,
            "symbol_overrides": dict(self.symbol_overrides),
        }


# ---------------------------------------------------------------------------
# Pre-trade check
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PreTradeRequest:
    """An incoming order intent to be validated before it may reach a venue."""

    id: str
    canonical_symbol: str
    side: RiskSide
    qty: int
    limit_price: float
    strategy_id: str = ""
    signal_id: str = ""
    created_ns: int = field(default_factory=now_ns)

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.limit_price

    @classmethod
    def from_dict(cls, body: Dict[str, Any]) -> "PreTradeRequest":
        symbol = body.get("symbol") or body.get("sym")
        if not symbol:
            raise ValueError("missing 'symbol' in pre-trade request")
        side_raw = str(body.get("side", "")).upper()
        if side_raw not in ("BUY", "SELL"):
            raise ValueError(f"invalid 'side' (need BUY/SELL): {body.get('side')!r}")
        try:
            qty = int(body["qty"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"missing or invalid 'qty': {exc}") from exc
        if qty == 0:
            raise ValueError("'qty' must be non-zero")
        try:
            limit_price = float(body.get("limit_px", body.get("limit_price", body.get("px", 0.0))))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"missing or invalid 'limit_px': {exc}") from exc
        return cls(
            id=str(body.get("id") or f"PRT-{now_ns()}"),
            canonical_symbol=str(symbol),
            side=RiskSide(side_raw),
            qty=qty,
            limit_price=limit_price,
            strategy_id=str(body.get("strategy_id", "")),
            signal_id=str(body.get("signal_id", "")),
        )


@dataclass(slots=True)
class CheckVerdict:
    """The outcome of a single named risk check."""

    name: str
    passed: bool
    severity: BreachSeverity
    detail: str = ""
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CheckResult:
    """Aggregate result of running all pre-trade checks for one request."""

    allowed: bool
    verdicts: List[CheckVerdict]
    breached: bool
    hard_breached: bool

    def to_dict(self, request: PreTradeRequest) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "breached": self.breached,
            "hard_breached": self.hard_breached,
            "request_id": request.id,
            "symbol": request.canonical_symbol,
            "side": request.side.value,
            "qty": request.qty,
            "limit_px": request.limit_price,
            "notional": round(request.notional, 4),
            "verdicts": [
                {
                    "name": v.name,
                    "passed": v.passed,
                    "severity": v.severity.value,
                    "detail": v.detail,
                    "context": v.context,
                }
                for v in self.verdicts
            ],
        }


# ---------------------------------------------------------------------------
# Breaches
# ---------------------------------------------------------------------------

_breach_counter = itertools.count(1)


def _next_breach_id() -> str:
    return f"BRK-{next(_breach_counter):08d}"


@dataclass(slots=True)
class BreachRecord:
    """A recorded limit breach (SOFT warning or HARD veto)."""

    id: str
    symbol: str
    check_name: str
    severity: BreachSeverity
    message: str
    context: Dict[str, Any] = field(default_factory=dict)
    ts_ns: int = field(default_factory=now_ns)
    escalated: bool = False

    @classmethod
    def create(cls, symbol: str, check_name: str, severity: BreachSeverity,
               message: str, context: Optional[Dict[str, Any]] = None) -> "BreachRecord":
        return cls(
            id=_next_breach_id(),
            symbol=symbol,
            check_name=check_name,
            severity=severity,
            message=message,
            context=context or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "check": self.check_name,
            "severity": self.severity.value,
            "message": self.message,
            "context": self.context,
            "ts_ns": self.ts_ns,
            "escalated": self.escalated,
        }
