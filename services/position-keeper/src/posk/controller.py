"""position_keeper — HTTP controller.

Endpoints (see dependency-map.json for S6):

    GET  /healthz              liveness
    GET  /readyz               readiness (engine initialized?)
    GET  /positions            all positions (query: account, include_flat)
    GET  /positions/snapshot   latest (or fresh) position snapshot with marks
    GET  /positions/{symbol}   one position by symbol (query: account)
    POST /adjust               manual adjustment to a position
    POST /corporate-action     apply a split/dividend corporate action
    GET  /history/{symbol}     bounded per-symbol event history
    GET  /stats                engine statistics
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .config import CONFIG
from .errors import (
    AdjustmentConflictError,
    DuplicateFillError,
    InvalidAdjustmentError,
    InvalidCorporateActionError,
    POSKError,
    UnknownPositionError,
    error_envelope,
)
from .models import now_ns

logger = logging.getLogger("posk.controller")


class PositionController:
    """Shared state bundle for the HTTP handlers."""

    def __init__(self) -> None:
        self.engine = None          # PositionEngine (wired by main)
        self.execution = None       # ExecutionClient (wired by main)
        self.books = None           # BookBuilderClient (wired by main)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def healthz(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok", "service": CONFIG.name, "version": CONFIG.version}

    def readyz(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(POSKError("position engine not initialized"))
        reasons = list(self.engine.readiness_reasons())
        body = {
            "status": "ready" if not reasons else "not_ready",
            "reasons": reasons,
            "positions_tracked": self.engine.stats_view()["positions_total"],
        }
        return (200 if not reasons else 503), body

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def positions(self, account: Optional[str] = None,
                  include_flat: bool = False) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(POSKError("position engine not initialized"))
        rows = self.engine.positions_view(account=account, include_flat=include_flat)
        return 200, {
            "count": len(rows),
            "positions": rows,
            "ts_ns": now_ns(),
        }

    def position_by_symbol(self, symbol: str,
                           account: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(POSKError("position engine not initialized"))
        try:
            pos = self.engine.get_position(account, symbol)
        except UnknownPositionError as exc:
            return 404, error_envelope(exc)
        ref = self.engine.reference_price(symbol)
        return 200, pos.to_dict(ref_price=ref)

    def snapshot(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(POSKError("position engine not initialized"))
        snap = self.engine.take_snapshot()
        return 200, snap.to_dict()

    # ------------------------------------------------------------------
    # Adjustments & corporate actions
    # ------------------------------------------------------------------

    def adjust(self, body: Optional[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(POSKError("position engine not initialized"))
        if body is None or not isinstance(body, dict):
            return 400, error_envelope(InvalidAdjustmentError(
                "POST /adjust requires a JSON object body"))
        try:
            pos = self.engine.apply_adjustment_dict(body)
        except InvalidAdjustmentError as exc:
            return 400, error_envelope(exc)
        except UnknownPositionError as exc:
            return 404, error_envelope(exc)
        ref = self.engine.reference_price(pos.symbol)
        return 200, {"status": "adjusted", "position": pos.to_dict(ref_price=ref)}

    def corporate_action(self, body: Optional[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(POSKError("position engine not initialized"))
        if body is None or not isinstance(body, dict):
            return 400, error_envelope(InvalidCorporateActionError(
                "POST /corporate-action requires a JSON object body"))
        try:
            affected = self.engine.apply_corporate_action_dict(body)
        except InvalidCorporateActionError as exc:
            return 400, error_envelope(exc)
        except UnknownPositionError as exc:
            return 404, error_envelope(exc)
        rows = [p.to_dict(ref_price=self.engine.reference_price(p.symbol)) for p in affected]
        return 200, {
            "status": "applied",
            "type": body.get("type"),
            "symbol": body.get("symbol"),
            "positions_affected": len(rows),
            "positions": rows,
        }

    # ------------------------------------------------------------------
    # History & stats
    # ------------------------------------------------------------------

    def history(self, symbol: str, limit: int = 100) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(POSKError("position engine not initialized"))
        entries = self.engine.history(symbol, limit=limit)
        return 200, {
            "symbol": symbol,
            "count": len(entries),
            "events": [e.to_dict() for e in entries],
        }

    def stats(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(POSKError("position engine not initialized"))
        return 200, {"engine": self.engine.stats_view()}
