"""position_keeper — position ledger core.

The :class:`PositionEngine` is the **authoritative** real-time position store.
It owns a map of :class:`~posk.models.PositionState` keyed by (account, symbol)
and enforces exactly-once fill application via an idempotency set.  All
mutations happen under a single lock so the ledger is safe to share between the
HTTP controller threads and the background ingest loop.

Responsibilities:

* **fill ingestion** — :meth:`apply_fill` applies each S4 fill exactly once,
  recording realized P&L and updating average-cost state;
* **manual adjustments** — :meth:`apply_adjustment` for operator corrections;
* **corporate actions** — :meth:`apply_corporate_action` for splits/dividends;
* **reference prices** — :meth:`set_reference_price` (from S2 top-of-book) used
  to mark positions to market in snapshots;
* **snapshots & history** — bounded per-symbol event history and periodic
  position snapshots for analytics (S9) and settlement (S14).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from .config import CONFIG, ServiceConfig
from .errors import (
    AdjustmentConflictError,
    DuplicateFillError,
    InvalidAdjustmentError,
    InvalidCorporateActionError,
    UnknownPositionError,
)
from .models import (
    AdjustmentRecord,
    CorporateAction,
    EventType,
    FillEvent,
    HistoryEntry,
    PositionKey,
    PositionSide,
    PositionSnapshot,
    PositionState,
    next_adjustment_id,
    now_ns,
)

logger = logging.getLogger("posk.position_engine")


class PositionEngine:
    """Thread-safe authoritative position ledger."""

    def __init__(self, cfg: Optional[ServiceConfig] = None) -> None:
        self.cfg = cfg or CONFIG
        self._lock = threading.RLock()
        # (account, symbol) -> PositionState
        self._positions: Dict[Tuple[str, str], PositionState] = {}
        # fill ids already applied (idempotency; bounded)
        self._seen_fill_ids: set = set()
        # symbol -> bounded deque of HistoryEntry
        self._history: Dict[str, Deque[HistoryEntry]] = {}
        # symbol -> reference price (from S2 top-of-book mid)
        self._ref_prices: Dict[str, float] = {}
        # periodic snapshots (bounded ring)
        self._snapshots: Deque[PositionSnapshot] = deque()
        self._history_seq = 0
        # statistics
        self.stats: Dict[str, int] = {
            "fills_applied": 0,
            "fills_deduplicated": 0,
            "adjustments_applied": 0,
            "corporate_actions_applied": 0,
            "snapshots_taken": 0,
        }

    # ------------------------------------------------------------------
    # Fill ingestion
    # ------------------------------------------------------------------

    def apply_fill(self, fill: FillEvent) -> PositionState:
        """Apply one fill to the ledger exactly once (idempotent by fill id).

        Raises :class:`DuplicateFillError` if the fill id was already applied.
        Returns the resulting :class:`PositionState`.
        """
        with self._lock:
            if fill.id in self._seen_fill_ids:
                self.stats["fills_deduplicated"] += 1
                raise DuplicateFillError(fill.id)

            key = (fill.account, fill.symbol)
            pos = self._positions.get(key)
            if pos is None:
                if len(self._positions) >= self.cfg.accounting.max_positions:
                    raise AdjustmentConflictError(
                        f"position capacity reached ({self.cfg.accounting.max_positions})",
                        context={"account": fill.account, "symbol": fill.symbol},
                    )
                pos = PositionState(account=fill.account, symbol=fill.symbol)
                self._positions[key] = pos

            realized = pos.apply_fill(fill.side, fill.qty, fill.price, fill.ts_ns)
            self._seen_fill_ids.add(fill.id)
            if len(self._seen_fill_ids) > 200_000:
                # Bound memory; fills are idempotent so dropping old ids is safe.
                self._seen_fill_ids = set(list(self._seen_fill_ids)[-100_000:])

            self.stats["fills_applied"] += 1
            self._record_history(
                fill.symbol, EventType.FILL, fill.id,
                {"side": fill.side.value, "qty": fill.qty, "price": fill.price,
                 "realized_pnl": round(realized, 6), "venue": fill.venue},
                fill.ts_ns,
            )
            logger.debug(
                "applied fill %s %s %s %d @ %.4f (net=%d avg=%.4f realized=%.4f)",
                fill.id, fill.account, fill.symbol, fill.qty, fill.price,
                pos.net_qty, pos.avg_price, pos.realized_pnl,
            )
            return pos

    def apply_fill_dict(self, body: Dict[str, Any]) -> PositionState:
        """Parse and apply a raw S4 fill wire dict (used by the ingest loop)."""
        try:
            fill = FillEvent.from_dict(body, self.cfg.accounting.default_account)
        except ValueError as exc:
            logger.warning("skipping malformed fill: %s", exc)
            raise InvalidAdjustmentError(str(exc)) from exc
        return self.apply_fill(fill)

    # ------------------------------------------------------------------
    # Manual adjustments
    # ------------------------------------------------------------------

    def apply_adjustment(self, adjustment: AdjustmentRecord) -> PositionState:
        """Apply an operator-initiated manual adjustment to a position."""
        with self._lock:
            key = (adjustment.account, adjustment.symbol)
            pos = self._positions.get(key)
            if pos is None:
                raise UnknownPositionError(adjustment.account, adjustment.symbol)
            pos.apply_adjustment(adjustment.delta_qty, adjustment.new_avg_price, adjustment.ts_ns)
            self.stats["adjustments_applied"] += 1
            self._record_history(
                adjustment.symbol, EventType.ADJUSTMENT, adjustment.id,
                {"delta_qty": adjustment.delta_qty, "reason": adjustment.reason,
                 "new_avg_price": adjustment.new_avg_price},
                adjustment.ts_ns,
            )
            logger.info(
                "applied adjustment %s to %s/%s delta=%d (net=%d)",
                adjustment.id, adjustment.account, adjustment.symbol,
                adjustment.delta_qty, pos.net_qty,
            )
            return pos

    def apply_adjustment_dict(self, body: Dict[str, Any]) -> PositionState:
        """Parse and apply a manual-adjustment request body."""
        try:
            adj = AdjustmentRecord.from_dict(body, self.cfg.accounting.default_account)
        except ValueError as exc:
            raise InvalidAdjustmentError(str(exc)) from exc
        return self.apply_adjustment(adj)

    # ------------------------------------------------------------------
    # Corporate actions
    # ------------------------------------------------------------------

    def apply_corporate_action(self, action: CorporateAction) -> List[PositionState]:
        """Apply a corporate action to every position in the affected symbol."""
        with self._lock:
            affected: List[PositionState] = []
            for (account, symbol), pos in self._positions.items():
                if symbol != action.symbol or pos.net_qty == 0:
                    continue
                pos.apply_corporate_action(action, action.ts_ns)
                affected.append(pos)
            if not affected:
                raise UnknownPositionError(self.cfg.accounting.default_account, action.symbol)
            self.stats["corporate_actions_applied"] += 1
            for pos in affected:
                self._record_history(
                    action.symbol, EventType.CORPORATE_ACTION, "",
                    {"type": action.type, "factor": action.factor,
                     "per_share": action.per_share},
                    action.ts_ns,
                )
            logger.info(
                "applied %s corporate action to %d position(s) of %s",
                action.type, len(affected), action.symbol,
            )
            return affected

    def apply_corporate_action_dict(self, body: Dict[str, Any]) -> List[PositionState]:
        """Parse and apply a corporate-action request body."""
        try:
            action = CorporateAction.from_dict(body)
        except ValueError as exc:
            raise InvalidCorporateActionError(str(exc)) from exc
        return self.apply_corporate_action(action)

    # ------------------------------------------------------------------
    # Reference prices
    # ------------------------------------------------------------------

    def set_reference_price(self, symbol: str, price: float) -> None:
        """Set the reference (mark) price for a symbol, used in snapshots."""
        if price <= 0:
            return
        with self._lock:
            self._ref_prices[symbol] = float(price)

    def reference_price(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._ref_prices.get(symbol)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_position(self, account: Optional[str], symbol: str) -> PositionState:
        """Fetch one position by (account, symbol); 404 if it does not exist."""
        with self._lock:
            key = (account or self.cfg.accounting.default_account, symbol)
            pos = self._positions.get(key)
            if pos is None:
                # Fall back to any account holding this symbol when no account given.
                if account is None:
                    for (acc, sym), p in self._positions.items():
                        if sym == symbol:
                            return p
                raise UnknownPositionError(key[0], symbol)
            return pos

    def list_positions(self, account: Optional[str] = None,
                       include_flat: bool = False) -> List[PositionState]:
        """All positions, optionally filtered by account; flat positions optional."""
        with self._lock:
            out: List[PositionState] = []
            for (acc, _sym), pos in self._positions.items():
                if account is not None and acc != account:
                    continue
                if not include_flat and pos.net_qty == 0:
                    continue
                out.append(pos)
            out.sort(key=lambda p: (p.account, p.symbol))
            return out

    def positions_view(self, account: Optional[str] = None,
                       include_flat: bool = False) -> List[Dict[str, Any]]:
        """Serialized position list with mark-to-market where a ref price exists."""
        positions = self.list_positions(account=account, include_flat=include_flat)
        out: List[Dict[str, Any]] = []
        for pos in positions:
            ref = self.reference_price(pos.symbol)
            out.append(pos.to_dict(ref_price=ref))
        return out

    # ------------------------------------------------------------------
    # Snapshots & history
    # ------------------------------------------------------------------

    def take_snapshot(self) -> PositionSnapshot:
        """Capture a point-in-time snapshot of all positions (with marks)."""
        with self._lock:
            ts = now_ns()
            rows: List[Dict[str, Any]] = []
            gross_notional = 0.0
            market_value = 0.0
            realized_pnl = 0.0
            unrealized_pnl = 0.0
            for pos in self.list_positions(account=None, include_flat=True):
                ref = self._ref_prices.get(pos.symbol)
                row = pos.to_dict(ref_price=ref)
                rows.append(row)
                gross_notional += abs(pos.net_qty) * (ref if ref is not None else pos.avg_price)
                market_value += pos.market_value(ref)
                realized_pnl += pos.realized_pnl
                unrealized_pnl += pos.unrealized_pnl(ref)

            snap = PositionSnapshot(
                ts_ns=ts,
                positions=rows,
                totals={
                    "positions": len(rows),
                    "gross_notional": round(gross_notional, 4),
                    "market_value": round(market_value, 4),
                    "realized_pnl": round(realized_pnl, 4),
                    "unrealized_pnl": round(unrealized_pnl, 4),
                },
            )
            self._snapshots.append(snap)
            while len(self._snapshots) > self.cfg.history.snapshot_retention:
                self._snapshots.popleft()
            self.stats["snapshots_taken"] += 1
            return snap

    def latest_snapshot(self) -> Optional[PositionSnapshot]:
        with self._lock:
            return self._snapshots[-1] if self._snapshots else None

    def history(self, symbol: str, limit: int = 100) -> List[HistoryEntry]:
        """Bounded per-symbol event history, newest first."""
        with self._lock:
            dq = self._history.get(symbol)
            if not dq:
                return []
            items = list(dq)
            items.reverse()
            if limit > 0 and len(items) > limit:
                items = items[:limit]
            return items

    # ------------------------------------------------------------------
    # Stats / readiness
    # ------------------------------------------------------------------

    def stats_view(self) -> Dict[str, Any]:
        with self._lock:
            open_positions = sum(1 for p in self._positions.values() if p.net_qty != 0)
            total_realized = sum(p.realized_pnl for p in self._positions.values())
            return {
                "positions_total": len(self._positions),
                "positions_open": open_positions,
                "total_realized_pnl": round(total_realized, 4),
                **self.stats,
            }

    def readiness_reasons(self) -> List[str]:
        """Return a flat list of reasons the engine is not ready (empty = ready)."""
        with self._lock:
            return []

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _record_history(self, symbol: str, kind: EventType, ref_id: str,
                        detail: Dict[str, Any], ts_ns: int) -> None:
        dq = self._history.get(symbol)
        if dq is None:
            dq = deque(maxlen=self.cfg.history.max_events_per_symbol)
            self._history[symbol] = dq
        self._history_seq += 1
        dq.append(HistoryEntry(seq=self._history_seq, kind=kind, ref_id=ref_id,
                               detail=detail, ts_ns=ts_ns))
