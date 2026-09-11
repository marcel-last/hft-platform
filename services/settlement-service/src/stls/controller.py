"""Request handlers for the settlement service (S14).

Each handler returns ``(status_code, body)`` (CONVENTIONS §3).  Handlers take
``_query`` and ``_body`` from the router plus any ``{param}`` path captures.
All service errors are raised as :class:`STLError` subclasses; the router is
the single mapping point to ``(http_status, error_envelope)``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .config import SettlementConfig
from .settlement_engine import SettlementEngine


class SettlementController:
    """Wires HTTP handlers to the :class:`SettlementEngine`."""

    def __init__(self, cfg: SettlementConfig, engine: SettlementEngine,
                 ingest_client=None) -> None:
        self.cfg = cfg
        self.engine = engine
        self.ingest_client = ingest_client

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def healthz(self) -> (int, Dict[str, Any]):  # type: ignore[valid-type]
        return 200, {
            "status": "ok",
            "service": self.cfg.name,
            "version": self.cfg.version,
        }

    def readyz(self) -> (int, Dict[str, Any]):  # type: ignore[valid-type]
        reasons = []
        if self.ingest_client is not None and self.cfg.ingest.enabled:
            # Consecutive (not cumulative) failures: a transient S6 blip that
            # the next pull recovers from must not strand readyz in not_ready.
            if self.ingest_client.consecutive_failures > 0:
                reasons.append(
                    f"position-keeper consecutive pull failures: "
                    f"{self.ingest_client.consecutive_failures}")
        return 200, {
            "status": "ready" if not reasons else "not_ready",
            "reasons": reasons,
        }

    # ------------------------------------------------------------------
    # Settle / finalize
    # ------------------------------------------------------------------

    def settle(self, body: Optional[Dict[str, Any]]) -> (int, Dict[str, Any]):  # type: ignore[valid-type]
        if body is None:
            from .errors import InvalidSettleRequest
            raise InvalidSettleRequest("The settle request requires a JSON body.",
                                       context={"field": "$"})
        result = self.engine.settle(body)
        return 200, result

    def finalize(self, date: str) -> (int, Dict[str, Any]):  # type: ignore[valid-type]
        result = self.engine.finalize_eod(date)
        return 200, result

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def report(self, date: str) -> (int, Dict[str, Any]):  # type: ignore[valid-type]
        return 200, self.engine.report(date)

    def run_status(self, date: str) -> (int, Dict[str, Any]):  # type: ignore[valid-type]
        # Same engine read as /reports, but the slim summary a dashboard polls.
        from .models import parse_settlement_date
        date = parse_settlement_date(date, self.engine.clock)
        with self.engine._lock:
            run = self.engine._get_run(date)
            summary = self.engine._run_summary(run)
        return 200, {"run": summary}

    def discrepancies(self, date: Optional[str], venue: Optional[str],
                      kind: Optional[str], limit_raw: Optional[str]) -> (int, Dict[str, Any]):  # type: ignore[valid-type]
        return 200, self.engine.discrepancies(date, venue, kind, limit_raw)

    def ingest_positions(self, body: Optional[Dict[str, Any]]) -> (int, Dict[str, Any]):  # type: ignore[valid-type]
        """Manual trigger: pull S6 now and settle the snapshot."""
        if self.ingest_client is None:
            from .errors import STLUpstreamError
            raise STLUpstreamError("The ingest client is not configured.",
                                   context={"detail": "position-keeper client absent"})
        rows = self.ingest_client.pull_positions()
        if rows is None:
            from .errors import STLUpstreamError
            raise STLUpstreamError(
                "Position pull from the position keeper failed.",
                context={"pull_failures": self.ingest_client.pull_failures})
        result = self.engine.ingest_from_positions(rows)
        return 200, {"ingested": True, **result}

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> (int, Dict[str, Any]):  # type: ignore[valid-type]
        out = self.engine.stats()
        if self.ingest_client is not None:
            out["ingest"] = {
                "enabled": self.cfg.ingest.enabled,
                "position_keeper_url": self.cfg.ingest.position_keeper_url,
                "interval_ms": self.cfg.ingest.interval_ms,
                "pull_successes": self.ingest_client.pull_successes,
                "pull_failures": self.ingest_client.pull_failures,
                "consecutive_failures": self.ingest_client.consecutive_failures,
            }
        else:
            out["ingest"] = {"enabled": False, "pull_successes": 0, "pull_failures": 0}
        return 200, out
