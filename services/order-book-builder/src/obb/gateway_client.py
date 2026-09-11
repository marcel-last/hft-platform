"""order_book_builder — market-data-gateway (S1) client.

Talks to the gateway's internal HTTP API to:

* subscribe to the configured symbol set,
* poll normalized quotes (the production deployment uses a push channel; this
  client models it with short-poll GETs against ``/quotes/{symbol}``),
* fetch full L2 snapshots for rebuilds (``/history/{symbol}`` + snapshot API).

All calls go through a tiny stdlib HTTP helper so the service has zero third-
party dependencies.  Timeouts and retries are configured in
:class:`~obb.config.IngestConfig`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .config import CONFIG
from .errors import GatewayUnreachableError

logger = logging.getLogger("obb.gateway_client")


class _HTTP:
    """Minimal blocking JSON HTTP helper (stdlib only)."""

    def __init__(self, base_url: str, timeout_ms: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_ms / 1000.0

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = {"error": {"message": payload[:200].decode("utf-8", "replace")}}
            if 400 <= exc.code < 500:
                raise GatewayUnreachableError(
                    f"gateway returned {exc.code} for {method} {path}",
                    context={"status": exc.code, "body": parsed},
                ) from exc
            raise GatewayUnreachableError(
                f"gateway error {exc.code} on {method} {path}",
                context={"status": exc.code},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GatewayUnreachableError(
                f"cannot reach gateway at {url}: {exc}",
                context={"url": url},
            ) from exc

    def get(self, path: str) -> Dict[str, Any]:
        return self._request("GET", path)

    def post(self, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        return self._request("POST", path, body)


class GatewayClient:
    """High-level client for the market-data-gateway internal API."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.ingest.gateway_url, CONFIG.ingest.request_timeout_ms)
        self.subscriber_id = "order-book-builder"
        self.connected = False

    # -- lifecycle -----------------------------------------------------------

    def subscribe(self, symbols: List[str]) -> Dict[str, Any]:
        """Register this service as a subscriber for ``symbols`` on the gateway."""
        resp = self.http.post("/subscribers", {
            "id": self.subscriber_id,
            "symbols": symbols,
        })
        self.connected = True
        logger.info("subscribed to %d symbols on gateway", len(symbols))
        return resp

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz")

    # -- data path -----------------------------------------------------------

    def poll_quotes(self, symbol: str, limit: int = 256) -> List[dict]:
        """Poll the latest normalized quotes for one symbol (non-consuming view)."""
        resp = self.http.get(f"/history/{symbol}?limit={limit}")
        return resp.get("quotes", [])

    def fetch_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch a full L2 snapshot for a symbol (for book rebuilds)."""
        try:
            resp = self.http.get(f"/snapshot/{symbol}")
        except GatewayUnreachableError as exc:
            logger.warning("snapshot fetch failed for %s: %s", symbol, exc.message)
            return None
        return resp.get("snapshot")

    def quality_report(self) -> Dict[str, Any]:
        return self.http.get("/quality")
