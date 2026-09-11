"""data_quality_monitor — HTTP controller (request handlers).

The controller is a thin layer over the :class:`~dqm.quality_engine.QualityEngine`.
It maps HTTP requests onto engine read-views and serializes failures using the
platform error envelope.  All handlers are synchronous.

Endpoints implemented here (see dependency-map.json for S8):

    GET  /healthz            liveness probe
    GET  /readyz             readiness probe (has an aggregation pass run?)
    GET  /quality-summary    unified dashboard view across all symbols
    GET  /quality/{symbol}   aggregated quality state for one symbol
    GET  /gaps               feed-gap and sequence-gap observations
    GET  /staleness          per-symbol staleness readings
    GET  /degradations       bounded degradation-episode history
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .config import CONFIG
from .errors import DQMErrors, UnknownSymbolError, error_envelope
from .models import now_ns

logger = logging.getLogger("dqm.controller")


class QualityController:
    """Bundles the shared service state that handlers need."""

    def __init__(self) -> None:
        # wired up by main.py after construction
        self.engine = None          # QualityEngine

    # ------------------------------------------------------------------
    # Health / readiness
    # ------------------------------------------------------------------

    def healthz(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok", "service": CONFIG.name, "version": CONFIG.version}

    def readyz(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(DQMErrors("quality engine not initialized"))
        reasons = list(self.engine.readiness_reasons())
        stats = self.engine.stats_view()
        body = {
            "status": "ready" if not reasons else "not_ready",
            "reasons": reasons,
            "symbols_tracked": stats["symbols_tracked"],
            "passes_completed": stats["passes_completed"],
        }
        return (200 if not reasons else 503), body

    # ------------------------------------------------------------------
    # Quality views
    # ------------------------------------------------------------------

    def quality_summary(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(DQMErrors("quality engine not initialized"))
        return 200, self.engine.summary()

    def quality_symbol(self, symbol: str) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(DQMErrors("quality engine not initialized"))
        sq = self.engine.symbol_view(symbol)
        if sq is None:
            return 404, error_envelope(UnknownSymbolError(symbol))
        return 200, {"ts_ns": now_ns(), "quality": sq.to_dict()}

    def gaps(self, limit: int = 100) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(DQMErrors("quality engine not initialized"))
        records = self.engine.gaps(limit=limit)
        return 200, {
            "ts_ns": now_ns(),
            "count": len(records),
            "gaps": [r.to_dict() for r in records],
        }

    def staleness(self, limit: int = 100) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(DQMErrors("quality engine not initialized"))
        samples = self.engine.staleness(limit=limit)
        return 200, {
            "ts_ns": now_ns(),
            "count": len(samples),
            "staleness": [s.to_dict() for s in samples],
        }

    def degradations(self, limit: int = 100,
                     kind: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(DQMErrors("quality engine not initialized"))
        events = self.engine.degradations(limit=limit, kind=kind)
        return 200, {
            "ts_ns": now_ns(),
            "count": len(events),
            "degradations": events,
        }

    def stats(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(DQMErrors("quality engine not initialized"))
        return 200, {"engine": self.engine.stats_view()}
