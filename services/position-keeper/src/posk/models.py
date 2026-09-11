"""position_keeper — core domain models.

Models in this module describe the position ledger's state and I/O:

* :class:`PositionSide` / :class:`EventType` — enums,
* :class:`PositionKey` — the (account, symbol) identity of a position,
* :class:`FillEvent` — a normalized execution report from S4,
* :class:`CorporateAction` — a split/dividend that reshapes qty and cost basis,
* :class:`AdjustmentRecord` — an operator-initiated manual correction,
* :class:`PositionState` — the authoritative net position with average-cost
  accounting (long/short aware),
* :class:`PositionSnapshot` — a point-in-time view of every position.

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

class PositionSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """Signed lot multiplier used in position math (long positive)."""
        return 1 if self is PositionSide.BUY else -1


class EventType(str, enum.Enum):
    FILL = "FILL"
    ADJUSTMENT = "ADJUSTMENT"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    SNAPSHOT = "SNAPSHOT"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PositionKey:
    """The (account, symbol) identity of an authoritative position."""

    account: str
    symbol: str

    @property
    def key(self) -> str:
        return f"{self.account}:{self.symbol}"


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FillEvent:
    """A normalized execution report as consumed by the position ledger."""

    id: str
    order_id: str
    symbol: str
    venue: str
    side: PositionSide
    qty: int
    price: float
    ts_ns: int
    account: str = ""

    @classmethod
    def from_dict(cls, body: Dict[str, Any], default_account: str) -> "FillEvent":
        """Parse an S4 fill wire dict (see execution-gateway ``Fill::to_json``)."""
        fid = str(body.get("id") or "").strip()
        if not fid:
            raise ValueError("missing 'id' in fill record")
        symbol = str(body.get("symbol") or body.get("sym") or "").strip()
        if not symbol:
            raise ValueError(f"fill {fid!r} is missing 'symbol'")
        side_raw = str(body.get("side", "")).upper()
        if side_raw not in ("BUY", "SELL"):
            raise ValueError(f"fill {fid!r} has invalid 'side': {body.get('side')!r}")
        try:
            qty = int(body.get("qty", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"fill {fid!r} has invalid 'qty': {exc}") from exc
        if qty <= 0:
            raise ValueError(f"fill {fid!r} has non-positive qty {qty}")
        try:
            price = float(body.get("price", body.get("px", 0.0)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"fill {fid!r} has invalid 'price': {exc}") from exc
        if price < 0:
            raise ValueError(f"fill {fid!r} has negative price {price}")
        ts_ns = int(body.get("ts_ns", body.get("vt", now_ns())))
        return cls(
            id=fid,
            order_id=str(body.get("order_id", "")),
            symbol=symbol,
            venue=str(body.get("venue", body.get("ven", "SIM"))),
            side=PositionSide(side_raw),
            qty=qty,
            price=price,
            ts_ns=ts_ns,
            account=str(body.get("account") or default_account) or default_account,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "venue": self.venue,
            "side": self.side.value,
            "qty": self.qty,
            "price": self.price,
            "ts_ns": self.ts_ns,
            "account": self.account,
        }


# ---------------------------------------------------------------------------
# Corporate actions & adjustments
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CorporateAction:
    """A corporate action that reshapes a position's quantity and cost basis.

    Supported types:

    * ``SPLIT`` — multiply ``qty`` by ``factor`` (e.g. 2.0 = 2-for-1) and divide
      the average entry price by the same factor; total market value preserved.
    * ``DIVIDEND`` — cash distribution of ``per_share`` per lot, credited to
      realized P&L without changing quantity or cost basis.

    ``factor`` is required for SPLIT (must be > 0); ``per_share`` is required
    for DIVIDEND.
    """

    type: str                 # "SPLIT" | "DIVIDEND"
    symbol: str
    factor: float = 1.0
    per_share: float = 0.0
    ts_ns: int = field(default_factory=now_ns)

    @classmethod
    def from_dict(cls, body: Dict[str, Any]) -> "CorporateAction":
        action_type = str(body.get("type", "")).upper()
        if action_type not in ("SPLIT", "DIVIDEND"):
            raise ValueError(f"invalid corporate-action 'type': {body.get('type')!r}")
        symbol = str(body.get("symbol") or body.get("sym") or "").strip()
        if not symbol:
            raise ValueError("corporate action is missing 'symbol'")
        factor = float(body.get("factor", 1.0))
        per_share = float(body.get("per_share", 0.0))
        if action_type == "SPLIT" and factor <= 0:
            raise ValueError("SPLIT corporate action requires factor > 0")
        if action_type == "DIVIDEND" and per_share < 0:
            raise ValueError("DIVIDEND corporate action requires per_share >= 0")
        return cls(
            type=action_type,
            symbol=symbol,
            factor=factor,
            per_share=per_share,
            ts_ns=int(body.get("ts_ns", now_ns())),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "symbol": self.symbol,
            "factor": self.factor,
            "per_share": self.per_share,
            "ts_ns": self.ts_ns,
        }


@dataclass(slots=True)
class AdjustmentRecord:
    """An operator-initiated manual correction to a position.

    ``delta_qty`` is signed (positive = increase the long / reduce the short).
    ``new_avg_price``, when provided, rebases the average entry price after the
    quantity change (used for cost-basis corrections); otherwise the existing
    average is preserved.
    """

    id: str
    account: str
    symbol: str
    delta_qty: int
    reason: str = ""
    new_avg_price: Optional[float] = None
    ts_ns: int = field(default_factory=now_ns)

    @classmethod
    def from_dict(cls, body: Dict[str, Any], default_account: str) -> "AdjustmentRecord":
        account = str(body.get("account") or default_account) or default_account
        symbol = str(body.get("symbol") or body.get("sym") or "").strip()
        if not symbol:
            raise ValueError("adjustment is missing 'symbol'")
        try:
            delta_qty = int(body.get("delta_qty", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid 'delta_qty': {exc}") from exc
        if delta_qty == 0:
            raise ValueError("'delta_qty' must be non-zero")
        new_avg_raw = body.get("new_avg_price", None)
        new_avg: Optional[float] = None
        if new_avg_raw is not None:
            try:
                new_avg = float(new_avg_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid 'new_avg_price': {exc}") from exc
            if new_avg < 0:
                raise ValueError("'new_avg_price' must be >= 0")
        return cls(
            id=str(body.get("id") or f"ADJ-{now_ns()}"),
            account=account,
            symbol=symbol,
            delta_qty=delta_qty,
            reason=str(body.get("reason", "")),
            new_avg_price=new_avg,
            ts_ns=int(body.get("ts_ns", now_ns())),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "account": self.account,
            "symbol": self.symbol,
            "delta_qty": self.delta_qty,
            "reason": self.reason,
            "new_avg_price": self.new_avg_price,
            "ts_ns": self.ts_ns,
        }


# ---------------------------------------------------------------------------
# Position state
# ---------------------------------------------------------------------------

@dataclass
class PositionState:
    """Authoritative net position and average-cost accounting for one (account, symbol).

    ``net_qty`` is signed (long positive, short negative).  ``avg_price`` is the
    volume-weighted average entry price of the open lots.  Realized P&L is
    accumulated on every reducing fill using standard average-cost semantics:
    a reduction realizes ``(fill_price - avg_price) * closed_qty`` in the
    direction of the position; a flip closes the old side at its average and
    re-opens the new side at the fill price.
    """

    account: str
    symbol: str
    net_qty: int = 0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    gross_cost: float = 0.0          # |net_qty| * avg_price (informational)
    last_fill_px: float = 0.0        # most recent fill price (mark fallback)
    fills_applied: int = 0
    adjustments_applied: int = 0
    created_ns: int = field(default_factory=now_ns)
    updated_ns: int = field(default_factory=now_ns)

    @property
    def side(self) -> str:
        if self.net_qty > 0:
            return "LONG"
        if self.net_qty < 0:
            return "SHORT"
        return "FLAT"

    @property
    def gross_notional(self) -> float:
        return abs(self.net_qty) * self.avg_price

    def apply_fill(self, side: PositionSide, qty: int, price: float, ts_ns: int) -> float:
        """Apply one fill using average-cost accounting.

        Returns the realized P&L generated by this fill (0.0 for opening/adds).
        A full flip closes the old side at its average and opens the new side at
        the fill price; a pure reduction keeps the surviving lots' original cost.
        """
        if qty <= 0:
            self.updated_ns = ts_ns
            return 0.0

        signed = side.sign * int(qty)
        prev_net = self.net_qty
        new_net = prev_net + signed
        realized = 0.0

        if prev_net == 0 or (prev_net > 0) == (signed > 0):
            # Opening fresh or adding to the same direction: re-average entry price.
            total_cost = abs(prev_net) * self.avg_price + abs(signed) * price
            self.avg_price = (total_cost / abs(new_net)) if new_net != 0 else 0.0
        else:
            # Reducing or flipping the position.
            closing = min(abs(prev_net), abs(signed))
            direction = 1 if prev_net > 0 else -1
            realized = (price - self.avg_price) * closing * direction
            remaining_signed = signed - direction * closing
            if remaining_signed == 0:
                # Fully closed: no open position, reset entry price.
                self.avg_price = 0.0
            elif abs(remaining_signed) < abs(prev_net):
                # Pure reduction: keep the original average cost of the survivors.
                pass
            else:
                # Flipped to the opposite side: rebase entry at this fill price.
                self.avg_price = price

        self.net_qty = new_net
        self.realized_pnl += realized
        self.gross_cost = abs(new_net) * self.avg_price
        self.last_fill_px = price
        self.fills_applied += 1
        self.updated_ns = ts_ns
        return realized

    def apply_adjustment(self, delta_qty: int, new_avg_price: Optional[float], ts_ns: int) -> None:
        """Apply a manual quantity adjustment (operator correction)."""
        prev_net = self.net_qty
        new_net = prev_net + int(delta_qty)
        if new_avg_price is not None:
            self.avg_price = float(new_avg_price)
        elif new_net == 0:
            self.avg_price = 0.0
        # Otherwise the average cost of the surviving lots is preserved as-is.
        self.net_qty = new_net
        self.gross_cost = abs(new_net) * self.avg_price
        self.adjustments_applied += 1
        self.updated_ns = ts_ns

    def apply_corporate_action(self, action: CorporateAction, ts_ns: int) -> float:
        """Apply a corporate action; returns the realized P&L impact (dividends)."""
        if action.type == "SPLIT":
            factor = action.factor
            self.net_qty = round(self.net_qty * factor)
            if self.avg_price > 0 and factor > 0:
                self.avg_price = self.avg_price / factor
            self.gross_cost = abs(self.net_qty) * self.avg_price
        elif action.type == "DIVIDEND":
            # Cash distribution on the open (long) lots only; credited to realized P&L.
            long_lots = max(0, self.net_qty)
            payout = long_lots * action.per_share
            self.realized_pnl += payout
        self.updated_ns = ts_ns
        return 0.0

    def market_value(self, ref_price: Optional[float] = None) -> float:
        """Mark the position to ``ref_price`` (falls back to last fill price)."""
        mark = ref_price if ref_price is not None else self.last_fill_px
        return self.net_qty * mark

    def unrealized_pnl(self, ref_price: Optional[float] = None) -> float:
        """Unrealized P&L vs the reference (or last-fill) mark."""
        if self.net_qty == 0:
            return 0.0
        mark = ref_price if ref_price is not None else self.last_fill_px
        return self.net_qty * (mark - self.avg_price)

    def to_dict(self, ref_price: Optional[float] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "account": self.account,
            "symbol": self.symbol,
            "net_qty": self.net_qty,
            "side": self.side,
            "avg_price": round(self.avg_price, 6),
            "gross_cost": round(self.gross_cost, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "last_fill_px": self.last_fill_px,
            "fills_applied": self.fills_applied,
            "adjustments_applied": self.adjustments_applied,
            "created_ns": self.created_ns,
            "updated_ns": self.updated_ns,
        }
        if ref_price is not None:
            body["ref_px"] = ref_price
            body["market_value"] = round(self.market_value(ref_price), 4)
            body["unrealized_pnl"] = round(self.unrealized_pnl(ref_price), 4)
        return body


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PositionSnapshot:
    """A point-in-time view of every position, with a reference-price map."""

    ts_ns: int
    positions: List[Dict[str, Any]]
    totals: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts_ns": self.ts_ns,
            "count": len(self.positions),
            "positions": self.positions,
            "totals": self.totals,
        }


# ---------------------------------------------------------------------------
# Event history record
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class HistoryEntry:
    """One bounded-history event for a symbol (fill/adjustment/corporate action)."""

    seq: int
    kind: EventType
    ref_id: str          # fill id / adjustment id / "" for corporate actions
    detail: Dict[str, Any]
    ts_ns: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind.value,
            "ref_id": self.ref_id,
            "detail": self.detail,
            "ts_ns": self.ts_ns,
        }


# ---------------------------------------------------------------------------
# Id generators (module-level counters)
# ---------------------------------------------------------------------------

_adjustment_counter = itertools.count(1)


def next_adjustment_id() -> str:
    return f"ADJ-{next(_adjustment_counter):08d}"


_snapshot_counter = itertools.count(1)


def next_snapshot_id() -> str:
    return f"SNAP-{next(_snapshot_counter):06d}"
