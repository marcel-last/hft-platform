"""portfolio_analytics — core domain models.

Models in this module describe the analytics state maintained by S9:

* :class:`PositionRow`   — one normalized position row pulled from S6,
* :class:`EquitySample`  — one point-in-time equity reading for the series,
* :class:`PnlRow`        — per-symbol realized + unrealized P&L view,
* :class:`AttributionRow`— one symbol's contribution to total P&L,
* :class:`PortfolioMetrics` — Sharpe / drawdown / win-rate aggregate,
* :class:`VarResult`     — VaR / CVaR result with method + sample count,

plus the ``now_ns()`` hot-path timestamp helper.  All timestamps are int64
nanoseconds since the Unix epoch; return series and equity values are floats
(the only place in the service where floating point is legitimate — it is
performance math, not latency math).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def now_ns() -> int:
    """Current time as int64 nanoseconds since the Unix epoch (hot-path convention)."""
    return time.time_ns()


# ---------------------------------------------------------------------------
# Normalized upstream rows
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PositionRow:
    """One position row as reported by the position-keeper (S6).

    Mirrors the S6 ``PositionState.to_dict`` wire shape.  ``market_value`` and
    ``unrealized_pnl`` are only present when S6 marked the position to a
    reference price; otherwise they default to zero and the engine falls back
    to last-fill marks.
    """

    symbol: str
    account: str = "MAIN"
    net_qty: int = 0
    side: str = "FLAT"                 # LONG | SHORT | FLAT
    avg_price: float = 0.0
    gross_cost: float = 0.0
    realized_pnl: float = 0.0
    last_fill_px: float = 0.0
    ref_px: Optional[float] = None     # S6 reference (mark) price, if provided
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    fills_applied: int = 0
    updated_ns: int = 0

    @classmethod
    def from_dict(cls, row: Dict[str, Any]) -> "PositionRow":
        """Parse one entry of the S6 ``/positions`` list."""
        symbol = str(row.get("symbol", ""))
        ref_raw = row.get("ref_px", None)
        try:
            ref_px = float(ref_raw) if ref_raw is not None else None
        except (TypeError, ValueError):
            ref_px = None
        return cls(
            symbol=symbol,
            account=str(row.get("account", "MAIN") or "MAIN"),
            net_qty=int(row.get("net_qty", 0) or 0),
            side=str(row.get("side", "FLAT")),
            avg_price=float(row.get("avg_price", 0.0) or 0.0),
            gross_cost=float(row.get("gross_cost", 0.0) or 0.0),
            realized_pnl=float(row.get("realized_pnl", 0.0) or 0.0),
            last_fill_px=float(row.get("last_fill_px", 0.0) or 0.0),
            ref_px=ref_px,
            market_value=float(row.get("market_value", 0.0) or 0.0),
            unrealized_pnl=float(row.get("unrealized_pnl", 0.0) or 0.0),
            fills_applied=int(row.get("fills_applied", 0) or 0),
            updated_ns=int(row.get("updated_ns", 0) or 0),
        )

    @property
    def mark(self) -> float:
        """Best available mark price for this position (ref, else last fill)."""
        return self.ref_px if self.ref_px is not None else self.last_fill_px

    @property
    def effective_market_value(self) -> float:
        """Market value recomputed from the best mark (robust to S6 fallbacks)."""
        return self.net_qty * self.mark

    @property
    def effective_unrealized(self) -> float:
        """Unrealized P&L recomputed from the best mark (robust to S6 fallbacks)."""
        if self.net_qty == 0:
            return 0.0
        return self.net_qty * (self.mark - self.avg_price)


# ---------------------------------------------------------------------------
# Equity series
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class EquitySample:
    """One point-in-time equity reading appended to the rolling series."""

    ts_ns: int
    equity: float                      # total portfolio equity (cash-neutral basis)
    market_value: float                # sum of |net_qty| * mark across positions
    realized_pnl: float                # cumulative realized P&L at this instant
    unrealized_pnl: float              # cumulative unrealized P&L at this instant

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts_ns": self.ts_ns,
            "equity": round(self.equity, 4),
            "market_value": round(self.market_value, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
        }


# ---------------------------------------------------------------------------
# P&L views
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PnlRow:
    """Per-symbol realized + unrealized P&L view."""

    symbol: str
    account: str = "MAIN"
    net_qty: int = 0
    side: str = "FLAT"
    avg_price: float = 0.0
    mark: float = 0.0
    market_value: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "account": self.account,
            "net_qty": self.net_qty,
            "side": self.side,
            "avg_price": round(self.avg_price, 6),
            "mark": round(self.mark, 6),
            "market_value": round(self.market_value, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "total_pnl": round(self.total_pnl, 4),
        }


@dataclass(slots=True)
class AttributionRow:
    """One symbol's contribution to total portfolio P&L."""

    symbol: str
    realized: float = 0.0
    unrealized: float = 0.0
    total: float = 0.0
    weight_pct: float = 0.0           # share of |total| across symbols (0..100)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "realized": round(self.realized, 4),
            "unrealized": round(self.unrealized, 4),
            "total": round(self.total, 4),
            "weight_pct": round(self.weight_pct, 4),
        }


# ---------------------------------------------------------------------------
# Aggregate metric results
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PortfolioMetrics:
    """Rolling performance metrics computed over the equity/return series."""

    samples: int = 0
    returns: int = 0
    sharpe_ratio: Optional[float] = None       # annualized; None if insufficient data
    max_drawdown_pct: float = 0.0              # worst peak-to-trough decline (negative)
    current_drawdown_pct: float = 0.0          # drawdown from the running peak
    win_rate_pct: Optional[float] = None       # % of positive returns; None if no returns
    mean_return: float = 0.0                   # per-period arithmetic mean return
    stdev_return: float = 0.0                  # per-period population stdev of returns
    annualized_return: Optional[float] = None  # compound annualized; None if <1 period

    def to_dict(self) -> Dict[str, Any]:
        return {
            "samples": self.samples,
            "returns": self.returns,
            "sharpe_ratio": round(self.sharpe_ratio, 6) if self.sharpe_ratio is not None else None,
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "current_drawdown_pct": round(self.current_drawdown_pct, 4),
            "win_rate_pct": round(self.win_rate_pct, 4) if self.win_rate_pct is not None else None,
            "mean_return": round(self.mean_return, 8),
            "stdev_return": round(self.stdev_return, 8),
            "annualized_return": (
                round(self.annualized_return, 6) if self.annualized_return is not None else None
            ),
        }


@dataclass(slots=True)
class VarResult:
    """Value-at-Risk / Conditional VaR result over the return series."""

    method: str                          # "historical" | "parametric"
    confidence: float                    # e.g. 0.95
    samples: int                         # number of returns used
    var: Optional[float] = None          # expected loss at the confidence level (positive)
    cvar: Optional[float] = None         # mean loss in the tail beyond VaR (positive)
    insufficient_data: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "confidence": round(self.confidence, 4),
            "samples": self.samples,
            "var": round(self.var, 8) if self.var is not None else None,
            "cvar": round(self.cvar, 8) if self.cvar is not None else None,
            "insufficient_data": self.insufficient_data,
        }
