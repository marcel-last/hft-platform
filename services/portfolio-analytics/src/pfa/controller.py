"""portfolio_analytics — HTTP controller (request handlers).

The controller is a thin layer over the :class:`~pfa.analytics_engine.AnalyticsEngine`.
It maps HTTP requests onto engine read-views and serializes failures using the
platform error envelope.  All handlers are synchronous.

Endpoints implemented here (see dependency-map.json for S9):

    GET  /healthz            liveness probe
    GET  /readyz             readiness probe (has a refresh pass run?)
    GET  /pnl                aggregate P&L + per-symbol breakdown
    GET  /pnl/{symbol}       per-symbol realized + unrealized P&L
    GET  /metrics            Sharpe, drawdown, win rate, annualized return
    GET  /attribution        per-symbol decomposition of total P&L
    GET  /var                VaR / CVaR (query: method, confidence)
    GET  /history            bounded equity/return series (query: limit)
    GET  /stats              engine statistics
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .config import CONFIG
from .errors import PFAError, UnknownSymbolError, error_envelope
from .models import now_ns

logger = logging.getLogger("pfa.controller")


class AnalyticsController:
    """Bundles the shared service state that handlers need."""

    def __init__(self) -> None:
        # wired up by main.py after construction
        self.engine = None          # AnalyticsEngine

    # ------------------------------------------------------------------
    # Health / readiness
    # ------------------------------------------------------------------

    def healthz(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok", "service": CONFIG.name, "version": CONFIG.version}

    def readyz(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(PFAError("analytics engine not initialized"))
        reasons = list(self.engine.readiness_reasons())
        stats = self.engine.stats_view()
        body = {
            "status": "ready" if not reasons else "not_ready",
            "reasons": reasons,
            "positions_tracked": stats["positions_tracked"],
            "refreshes_completed": stats["refreshes_completed"],
        }
        return (200 if not reasons else 503), body

    # ------------------------------------------------------------------
    # P&L views
    # ------------------------------------------------------------------

    def pnl(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(PFAError("analytics engine not initialized"))
        return 200, self.engine.pnl()

    def pnl_symbol(self, symbol: str) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(PFAError("analytics engine not initialized"))
        row = self.engine.pnl_symbol(symbol)
        if row is None:
            return 404, error_envelope(UnknownSymbolError(symbol))
        return 200, {"ts_ns": now_ns(), "pnl": row.to_dict()}

    # ------------------------------------------------------------------
    # Metrics / attribution / risk
    # ------------------------------------------------------------------

    def metrics(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(PFAError("analytics engine not initialized"))
        m = self.engine.metrics()
        return 200, {"ts_ns": now_ns(), "metrics": m.to_dict()}

    def attribution(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(PFAError("analytics engine not initialized"))
        return 200, self.engine.attribution()

    def var(self, method: Optional[str] = None,
            confidence: Optional[float] = None) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(PFAError("analytics engine not initialized"))
        result = self.engine.var(method=method, confidence=confidence)
        status = 200
        body: Dict[str, Any] = {"ts_ns": now_ns(), "var": result.to_dict()}
        if result.insufficient_data:
            # Still return the (partial) result; the flag tells callers to wait.
            body["note"] = "insufficient return history for a reliable VaR estimate"
        return status, body

    def history(self, limit: int = 100) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(PFAError("analytics engine not initialized"))
        samples = self.engine.history(limit=limit)
        return 200, {
            "ts_ns": now_ns(),
            "count": len(samples),
            "series": samples,
        }

    def stats(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(PFAError("analytics engine not initialized"))
        return 200, {"engine": self.engine.stats_view()}
