"""risk_manager — HTTP controller.

Endpoints (see dependency-map.json for S5):

    GET  /healthz             liveness
    GET  /readyz              readiness (engine initialized?)
    POST /pre-trade-check     validate an order intent against all risk limits
    GET  /limits              current effective risk limits
    PUT  /limits              update risk limits (partial or full)
    GET  /exposure            portfolio exposure snapshot (positions + open orders)
    POST /kill-switch         engage the kill-switch (body: {reason})
    POST /kill-switch/disengage   disarm the kill-switch
    GET  /kill-switch         current kill-switch status
    GET  /breaches            recent risk breaches (filter by severity/limit)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG
from .errors import (
    InvalidPreTradeRequestError,
    KillSwitchAlreadyEngagedError,
    KillSwitchNotEngagedError,
    RKMError,
    error_envelope,
)
from .models import PreTradeRequest, RiskSide, now_ns

logger = logging.getLogger("rkm.controller")


class RiskController:
    """Shared state bundle for the HTTP handlers."""

    def __init__(self) -> None:
        self.engine = None          # RiskEngine (wired by main)
        self.execution = None       # ExecutionClient (wired by main)
        self.alerting = None        # AlertingClient (wired by main)
        self.audit = None           # AuditClient (wired by main)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def healthz(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok", "service": CONFIG.name, "version": CONFIG.version}

    def readyz(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        ks = self.engine.kill_switch_status()
        body = {
            "status": "ready",
            "kill_switch": ks["state"],
            "positions_tracked": len(self.engine._positions),
        }
        return 200, body

    # ------------------------------------------------------------------
    # Pre-trade check
    # ------------------------------------------------------------------

    def pre_trade_check(self, body: Optional[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        try:
            request = PreTradeRequest.from_dict(body or {})
        except ValueError as exc:
            return 400, error_envelope(InvalidPreTradeRequestError(str(exc)))

        result = self.engine.pre_trade_check(request)
        payload = result.to_dict(request)
        payload["checked_ns"] = now_ns()

        # Escalate HARD breaches downstream (best effort; never blocks the response).
        if result.hard_breached:
            self._escalate_hard(request, result)

        status = 200 if result.allowed else 409
        return status, payload

    def _escalate_hard(self, request: PreTradeRequest, result) -> None:
        hard_verdicts = [v for v in result.verdicts
                         if not v.passed and v.severity.value == "HARD"]
        detail = "; ".join(v.detail for v in hard_verdicts)
        alert = {
            "source": CONFIG.name,
            "severity": "CRITICAL",
            "title": f"pre-trade veto: {request.canonical_symbol}",
            "detail": detail,
            "context": {
                "request_id": request.id,
                "symbol": request.canonical_symbol,
                "side": request.side.value,
                "qty": request.qty,
                "limit_px": request.limit_price,
            },
        }
        if self.alerting is not None:
            try:
                self.alerting.send_alert(alert)
            except RKMError as exc:
                logger.warning("failed to escalate breach to alerting service: %s", exc.message)
        if self.audit is not None:
            try:
                self.audit.record_event({
                    "event_type": "RISK_VETO",
                    "actor": CONFIG.name,
                    "symbol": request.canonical_symbol,
                    "detail": detail,
                    "ts_ns": now_ns(),
                })
            except RKMError as exc:
                logger.warning("failed to record veto in audit log: %s", exc.message)

    # ------------------------------------------------------------------
    # Limits
    # ------------------------------------------------------------------

    def get_limits(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        body = self.engine.limits.to_dict()
        body["velocity"] = {
            "window_ms": CONFIG.velocity.window_ms,
            "max_orders_per_symbol": CONFIG.velocity.max_orders_per_symbol,
            "max_orders_total": CONFIG.velocity.max_orders_total,
        }
        body["max_open_orders"] = CONFIG.kill_switch.max_open_orders
        return 200, body

    def put_limits(self, body: Optional[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        if body is None or not isinstance(body, dict):
            return 400, error_envelope(InvalidPreTradeRequestError("PUT /limits requires a JSON object body"))
        limits = self.engine.update_limits(body)
        logger.info("risk limits updated: %s", {k: v for k, v in limits.to_dict().items() if k != "symbol_overrides"})
        return 200, {"status": "updated", "limits": limits.to_dict()}

    def reset_limits(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        limits = self.engine.reset_limits()
        logger.info("risk limits reset to defaults")
        return 200, {"status": "reset", "limits": limits.to_dict()}

    # ------------------------------------------------------------------
    # Exposure
    # ------------------------------------------------------------------

    def exposure(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        body = self.engine.exposure()
        body["limits"] = {
            "max_portfolio_notional": self.engine.limits.max_portfolio_notional,
            "max_open_orders": CONFIG.kill_switch.max_open_orders,
        }
        return 200, body

    # ------------------------------------------------------------------
    # Kill-switch
    # ------------------------------------------------------------------

    def engage_kill_switch(self, body: Optional[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        reason = str((body or {}).get("reason", "manual"))
        try:
            self.engine.engage_kill_switch(reason)
        except KillSwitchAlreadyEngagedError as exc:
            return 409, error_envelope(exc)

        # Flatten open orders at the venue boundary (best effort).
        flatten_result: Optional[Dict[str, Any]] = None
        if CONFIG.kill_switch.flatten_on_engage and self.execution is not None:
            open_ids = [o.id for o in self.engine.open_orders() if o.is_open]
            if open_ids:
                try:
                    flatten_result = self.execution.flatten(open_ids)
                except RKMError as exc:
                    logger.warning("flatten failed during kill-switch: %s", exc.message)

        # Audit + alert the activation.
        if self.audit is not None:
            try:
                self.audit.record_event({
                    "event_type": "KILL_SWITCH",
                    "actor": CONFIG.name,
                    "action": "engage",
                    "reason": reason,
                    "ts_ns": now_ns(),
                })
            except RKMError as exc:
                logger.warning("failed to record kill-switch in audit log: %s", exc.message)
        if self.alerting is not None:
            try:
                self.alerting.send_alert({
                    "source": CONFIG.name,
                    "severity": "CRITICAL",
                    "title": "kill-switch ENGAGED",
                    "detail": reason,
                    "context": {"flatten": flatten_result or {}},
                })
            except RKMError as exc:
                logger.warning("failed to alert kill-switch activation: %s", exc.message)

        return 200, {
            "status": "engaged",
            "reason": reason,
            "kill_switch": self.engine.kill_switch_status(),
            "flatten": flatten_result or {"cancelled": 0, "failed": 0},
        }

    def disengage_kill_switch(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        try:
            self.engine.disengage_kill_switch()
        except KillSwitchNotEngagedError as exc:
            return 409, error_envelope(exc)
        if self.audit is not None:
            try:
                self.audit.record_event({
                    "event_type": "KILL_SWITCH",
                    "actor": CONFIG.name,
                    "action": "disengage",
                    "ts_ns": now_ns(),
                })
            except RKMError as exc:
                logger.warning("failed to record kill-switch disengage in audit log: %s", exc.message)
        return 200, {"status": "disarmed", "kill_switch": self.engine.kill_switch_status()}

    def kill_switch_status(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        return 200, self.engine.kill_switch_status()

    # ------------------------------------------------------------------
    # Breaches / stats
    # ------------------------------------------------------------------

    def breaches(self, limit: int = 100, severity: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        records = self.engine.breaches(limit=limit, severity=severity)
        return 200, {
            "count": len(records),
            "breaches": [r.to_dict() for r in records],
        }

    def stats(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(RKMError("risk engine not initialized"))
        return 200, {"engine": self.engine.stats_view()}
