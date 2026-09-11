"""strategy_engine — HTTP controller.

Endpoints (see ``dependency-map.json`` for S3):

    GET    /healthz              liveness
    GET    /readyz               readiness (engine initialized + at least one tick)
    GET    /signals              recent signals (optional ?symbol= & ?limit=)
    GET    /signals/{signal_id}  one signal by id
    GET    /strategies           registered strategies with state + stats
    POST   /strategies/pause     pause a strategy (?id= or {"id": ...})
    POST   /strategies/resume    resume a strategy
    POST   /pause                pause all strategies
    POST   /resume               resume all strategies
    GET    /intents              recent order intents (optional ?limit=)
    GET    /stats                engine statistics + per-strategy state
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from .config import CONFIG
from .errors import STEError, UnknownStrategyError, error_envelope
from .models import SignalStatus

logger = logging.getLogger("ste.controller")


class StrategyController:
    """Shared state bundle for the HTTP handlers."""

    def __init__(self) -> None:
        self.engine = None          # SignalEngine (wired by main)
        self.gateway = None         # GatewayClient (wired by main)
        self.books = None           # BookBuilderClient (wired by main)
        self.execution = None       # ExecutionClient (wired by main)

    def _require_engine(self):
        if self.engine is None:
            raise STEError("engine not initialized")
        return self.engine

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def healthz(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok", "service": CONFIG.name, "version": CONFIG.version}

    def readyz(self) -> Tuple[int, Dict[str, Any]]:
        engine = self.engine
        if engine is None:
            return 503, error_envelope(STEError("engine not initialized"))
        strategies = len(engine._strategies)
        ticks = engine.stats["ticks_processed"]
        reasons: list = []
        if strategies == 0:
            reasons.append("no strategies registered")
        if ticks == 0:
            reasons.append("no market data ingested yet")
        ready = not reasons
        return (200 if ready else 503), {
            "status": "ready" if ready else "not_ready",
            "reasons": reasons,
            "strategies": strategies,
            "ticks_processed": ticks,
        }

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def signals(self, symbol: str = "", limit: int = 100) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        sym = symbol or None
        items = engine.signals(symbol=sym, limit=limit)
        return 200, {
            "count": len(items),
            "signals": [s.to_dict() for s in items],
        }

    def signal_by_id(self, signal_id: str) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        sig = engine.get_signal(signal_id)
        if sig is None:
            return 404, error_envelope(STEError(f"no signal with id {signal_id!r}"))
        return 200, {"signal": sig.to_dict()}

    # ------------------------------------------------------------------
    # Strategies
    # ------------------------------------------------------------------

    def strategies(self) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        rows = []
        for sid, strategy in engine._strategies.items():
            rows.append({
                "id": sid,
                "name": strategy.name,
                "state": getattr(strategy, "state", "ACTIVE"),
            })
        return 200, {"count": len(rows), "strategies": rows}

    def pause_strategy(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        sid = (body or {}).get("id")
        if not sid or engine.pause_strategy(sid) is False:
            return 404, error_envelope(UnknownStrategyError(sid or ""))
        return 200, {"status": "paused", "strategy_id": sid}

    def resume_strategy(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        sid = (body or {}).get("id")
        if not sid or engine.resume_strategy(sid) is False:
            return 404, error_envelope(UnknownStrategyError(sid or ""))
        return 200, {"status": "resumed", "strategy_id": sid}

    def pause_all(self) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        ids = list(engine._strategies.keys())
        for sid in ids:
            engine.pause_strategy(sid)
        return 200, {"status": "paused", "count": len(ids), "strategy_ids": ids}

    def resume_all(self) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        ids = list(engine._strategies.keys())
        for sid in ids:
            engine.resume_strategy(sid)
        return 200, {"status": "resumed", "count": len(ids), "strategy_ids": ids}

    # ------------------------------------------------------------------
    # Intents / stats
    # ------------------------------------------------------------------

    def intents(self, limit: int = 100) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        items = engine.intents(limit=limit)
        return 200, {
            "count": len(items),
            "intents": [i.to_dict() for i in items],
        }

    def stats(self) -> Tuple[int, Dict[str, Any]]:
        engine = self._require_engine()
        open_signals = sum(1 for s in engine.signals(limit=4096) if s.status == SignalStatus.OPEN)
        return 200, {
            "engine": dict(engine.stats),
            "strategies": engine.strategy_states(),
            "open_signals": open_signals,
        }
