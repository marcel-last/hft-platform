"""Net position and net cash computation for settlement reports (S14).

The netting layer consumes a run's validated :class:`FillRecord` list and
produces:

* **net positions** — per (account, symbol): BUY/SELL quantities and
  notionals, the signed net quantity, and the net cash flow
  (sell proceeds minus buy outlay).  Rows that are flat *and* carry no net
  notional are omitted when ``zero_out_flat_positions`` is set, so a busy day
  that ends flat does not flood the report with zero rows.
* **cash summary** — the day's aggregate: total buy notional, total sell
  notional, total fees, and ``net_cash = sell_notional − buy_notional − fees``.

Position rows are capped at ``max_net_positions_per_run`` (newest keys first
after a deterministic sort), keeping a report's size bounded no matter how
many symbols settled.  All arithmetic is plain float; rounding happens only at
the serialization boundary (``round(x, 6)`` on notionals), never in the
comparison paths.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import SettlementConfig
from .models import FillRecord


@dataclass
class NetPosition:
    """Aggregated settled position for one (account, symbol)."""

    account: str
    symbol: str
    buy_qty: int
    sell_qty: int
    net_qty: int          # buy_qty - sell_qty
    buy_notional: float
    sell_notional: float
    net_cash_flow: float  # sell_notional - buy_notional (fees excluded)
    fees: float
    fill_count: int
    avg_fill_px: float    # weighted average price over the day's turnover

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "symbol": self.symbol,
            "buy_qty": self.buy_qty,
            "sell_qty": self.sell_qty,
            "net_qty": self.net_qty,
            "buy_notional": round(self.buy_notional, 6),
            "sell_notional": round(self.sell_notional, 6),
            "net_cash_flow": round(self.net_cash_flow, 6),
            "fees": round(self.fees, 6),
            "fill_count": self.fill_count,
            "avg_fill_px": round(self.avg_fill_px, 10),
        }


@dataclass
class CashSummary:
    """Day-level cash aggregation across all positions."""

    buy_notional: float
    sell_notional: float
    total_fees: float
    net_cash: float       # sell_notional - buy_notional - total_fees
    fill_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "buy_notional": round(self.buy_notional, 6),
            "sell_notional": round(self.sell_notional, 6),
            "total_fees": round(self.total_fees, 6),
            "net_cash": round(self.net_cash, 6),
            "fill_count": self.fill_count,
        }


#: Below this many currency units a "net notional" counts as flat.
_FLAT_NOTIONAL_EPS = 1e-6


def _is_flat(pos: NetPosition) -> bool:
    """A row is flat when it has zero net quantity and ~zero net notional."""
    return pos.net_qty == 0 and abs(pos.net_cash_flow) < _FLAT_NOTIONAL_EPS


def compute_net_positions(
    fills: List[FillRecord],
    cfg: SettlementConfig,
) -> List[NetPosition]:
    """Aggregate ``fills`` into per-(account, symbol) net positions.

    Rows are sorted by (account, symbol) for stable output.  Flat rows are
    dropped per ``cfg.netting.zero_out_flat_positions``; the surviving list is
    capped at ``cfg.netting.max_net_positions_per_run``.
    """
    agg: Dict[tuple, NetPosition] = {}
    for f in fills:
        key = (f.account, f.symbol)
        pos = agg.get(key)
        notional = f.px * f.qty
        if pos is None:
            pos = NetPosition(
                account=f.account, symbol=f.symbol,
                buy_qty=0, sell_qty=0, net_qty=0,
                buy_notional=0.0, sell_notional=0.0, net_cash_flow=0.0,
                fees=0.0, fill_count=0, avg_fill_px=0.0)
            agg[key] = pos
        if f.side == "BUY":
            pos.buy_qty += f.qty
            pos.buy_notional += notional
        else:
            pos.sell_qty += f.qty
            pos.sell_notional += notional
        pos.fees += f.fee
        pos.fill_count += 1

    rows: List[NetPosition] = []
    for (account, symbol) in sorted(agg):
        pos = agg[(account, symbol)]
        pos.net_qty = pos.buy_qty - pos.sell_qty
        pos.net_cash_flow = pos.sell_notional - pos.buy_notional
        total_qty = pos.buy_qty + pos.sell_qty
        total_notional = pos.buy_notional + pos.sell_notional
        pos.avg_fill_px = (total_notional / total_qty) if total_qty > 0 else 0.0
        if cfg.netting.zero_out_flat_positions and _is_flat(pos):
            continue
        rows.append(pos)

    cap = cfg.netting.max_net_positions_per_run
    if len(rows) > cap:
        rows = rows[:cap]
    return rows


def compute_cash_summary(fills: List[FillRecord]) -> CashSummary:
    """Aggregate the run's fills into the day-level cash summary."""
    buy_notional = 0.0
    sell_notional = 0.0
    fees = 0.0
    for f in fills:
        notional = f.px * f.qty
        if f.side == "BUY":
            buy_notional += notional
        else:
            sell_notional += notional
        fees += f.fee
    return CashSummary(
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        total_fees=fees,
        net_cash=sell_notional - buy_notional - fees,
        fill_count=len(fills),
    )
