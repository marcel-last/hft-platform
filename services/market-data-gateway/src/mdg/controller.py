"""market_data_gateway — HTTP controller (request handlers).

The controller is a thin layer over the service's in-process state.  It maps
HTTP requests onto service operations and serializes responses using the
platform error envelope for failures.  All handlers are synchronous; the
service runs them on a small thread pool (see ``main.py``).

Endpoints implemented here:

    GET  /healthz          liveness probe
    GET  /readyz           readiness probe (feeds streaming?)
    GET  /feeds            per-venue connection status report
    GET  /symbols          canonical symbol table with tick metadata
    GET  /quotes/{symbol}  latest N quotes for a symbol (from the ring buffer)
    GET  /quality          rolling quality report from the quality monitor
    POST /resync           force a snapshot resync of all venue trackers
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .config import CONFIG
from .errors import MDGError, error_envelope, SubscriberNotFoundError
from .models import ControlEventKind

logger = logging.getLogger("mdg.controller")


class GatewayController:
    """Bundles the shared service state that handlers need."""

    def __init__(self) -> None:
        # these are wired up by main.py after construction
        self.feed_client = None            # FeedClient
        self.normalizer = None             # QuoteNormalizer
        self.quality_monitor = None        # QualityMonitor
        self.quote_buffer = None           # ShardedRingBuffer
        self.subscribers: Dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Health / readiness
    # ------------------------------------------------------------------

    def healthz(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok", "service": CONFIG.name, "version": CONFIG.version}

    def readyz(self) -> Tuple[int, Dict[str, Any]]:
        if self.feed_client is None:
            return 503, {"status": "not_ready", "reason": "feed client not initialized"}
        report = self.feed_client.status_report()
        streaming = [k for k, v in report.items() if v["status"] == "STREAMING"]
        dead = [k for k, v in report.items() if v["status"] == "DEAD"]
        body: Dict[str, Any] = {
            "status": "ready" if streaming else "degraded",
            "streaming_venues": sorted(streaming),
            "dead_venues": sorted(dead),
        }
        code = 200 if streaming else 503
        return code, body

    # ------------------------------------------------------------------
    # Feed / symbol introspection
    # ------------------------------------------------------------------

    def feeds(self) -> Tuple[int, Dict[str, Any]]:
        if self.feed_client is None:
            return 503, error_envelope(MDGError("feed client not initialized"))
        return 200, {"venues": self.feed_client.status_report()}

    def symbols(self) -> Tuple[int, Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen = set()
        for (venue, venue_symbol), canonical in sorted(CONFIG.normalization.canonical_map.items()):
            if canonical in seen:
                continue
            seen.add(canonical)
            tick = CONFIG.normalization.tick_sizes.get(canonical)
            rows.append({
                "canonical_symbol": canonical,
                "tick_size": tick[0] if tick else None,
                "price_decimals": tick[1] if tick else None,
                "qty_decimals": tick[2] if tick else None,
                "venues": [v for (v, s), c in CONFIG.normalization.canonical_map.items() if c == canonical],
            })
        return 200, {"count": len(rows), "symbols": rows}

    # ------------------------------------------------------------------
    # Quote access
    # ------------------------------------------------------------------

    def latest_quotes(self, symbol: str, limit: int = 50) -> Tuple[int, Dict[str, Any]]:
        if self.quote_buffer is None:
            return 503, error_envelope(MDGError("quote buffer not initialized"))
        quotes = self.quote_buffer.drain(symbol, max_items=limit)
        # drain() consumes; for a read-only API view we re-emit nothing — the
        # gateway keeps a small per-symbol recent cache instead (see below).
        return 200, {
            "symbol": symbol,
            "count": len(quotes),
            "quotes": [q if isinstance(q, dict) else q for q in quotes],
        }

    def quote_history(self, symbol: str, limit: int = 100) -> Tuple[int, Dict[str, Any]]:
        """Non-consuming view of the most recent quotes (kept by main loop)."""
        history: List[dict] = getattr(self, "_recent", {}).get(symbol, [])[-limit:]
        return 200, {"symbol": symbol, "count": len(history), "quotes": history}

    # ------------------------------------------------------------------
    # Quality / control
    # ------------------------------------------------------------------

    def quality_report(self) -> Tuple[int, Dict[str, Any]]:
        if self.quality_monitor is None:
            return 503, error_envelope(MDGError("quality monitor not initialized"))
        return 200, {
            "symbols": self.quality_monitor.report(),
            "staleness_breaches_total": self.quality_monitor.staleness_breaches_total,
            "symbols_tracked": self.quality_monitor.symbols_tracked,
        }

    def resync(self) -> Tuple[int, Dict[str, Any]]:
        if self.normalizer is None or self.quality_monitor is None:
            return 503, error_envelope(MDGError("service not initialized"))
        self.normalizer.reset_trackers()
        self.quality_monitor.reset()
        logger.warning("manual resync requested; sequence trackers and quality windows cleared")
        return 200, {"status": "resynced", "detail": "trackers and quality windows reset"}

    def subscribers(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {
            "count": len(self.subscribers),
            "subscribers": [
                {"id": sid, "symbols": info.get("symbols", []), "connected_at_ns": info.get("connected_at_ns")}
                for sid, info in sorted(self.subscribers.items())
            ],
        }

    def unsubscribe(self, subscriber_id: str) -> Tuple[int, Dict[str, Any]]:
        if subscriber_id not in self.subscribers:
            return 404, error_envelope(SubscriberNotFoundError(subscriber_id))
        del self.subscribers[subscriber_id]
        logger.info("subscriber %s removed", subscriber_id)
        return 200, {"status": "removed", "subscriber_id": subscriber_id}
