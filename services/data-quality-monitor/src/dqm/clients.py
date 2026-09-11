"""data_quality_monitor — upstream HTTP clients.

Talks to three neighbours using only the stdlib (``urllib``):

* :class:`GatewayClient`    — S1 market-data-gateway: pull ``/quality`` for the
  per-symbol feed metrics (staleness, gaps, ordering violations).
* :class:`BookBuilderClient`— S2 order-book-builder: pull ``/books`` for the
  per-(symbol, venue) book health and cross-event counters.
* :class:`AlertingClient`   — S10 alerting-service: fire-and-forget POST of
  degradation episodes (best-effort; failures are swallowed by the caller).

All three share the tiny blocking :class:`_HTTP` helper so the service has zero
third-party dependencies.  Timeouts come from :class:`~dqm.config`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .config import CONFIG
from .errors import UpstreamUnreachableError

logger = logging.getLogger("dqm.clients")


class _HTTP:
    """Minimal blocking JSON HTTP helper (stdlib only)."""

    def __init__(self, base_url: str, timeout_ms: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_ms / 1000.0

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 target: str = "upstream") -> Dict[str, Any]:
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
            raise UpstreamUnreachableError(
                target, f"{exc.code} on {method} {path}",
                context={"status": exc.code, "body": parsed},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpstreamUnreachableError(target, str(exc), context={"url": url}) from exc

    def get(self, path: str, target: str = "upstream") -> Dict[str, Any]:
        return self._request("GET", path, target=target)

    def post(self, path: str, body: Optional[dict] = None,
             target: str = "upstream") -> Dict[str, Any]:
        return self._request("POST", path, body, target=target)


class GatewayClient:
    """Client for the market-data-gateway (S1)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.ingest.market_data_gateway_url,
                          CONFIG.ingest.request_timeout_ms)
        self.target = "market-data-gateway"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def quality_report(self) -> Dict[str, Any]:
        """Fetch the rolling per-symbol quality report (``GET /quality``)."""
        return self.http.get("/quality", target=self.target)


class BookBuilderClient:
    """Client for the order-book-builder (S2)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.ingest.order_book_builder_url,
                          CONFIG.ingest.request_timeout_ms)
        self.target = "order-book-builder"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def books(self) -> List[Dict[str, Any]]:
        """Fetch all maintained books with health + ToB (``GET /books``)."""
        resp = self.http.get("/books", target=self.target)
        return resp.get("books", [])


class AlertingClient:
    """Best-effort client for the alerting-service (S10)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.alerting.alert_service_url,
                          CONFIG.alerting.alert_timeout_ms)
        self.target = "alerting-service"

    def send_degradation(self, event_dict: Dict[str, Any]) -> bool:
        """POST one degradation episode; returns True on success.

        Never raises — the caller treats alert fan-out as fire-and-forget and a
        down S10 must not block the aggregation loop.
        """
        if not CONFIG.alerting.send_alerts:
            return False
        try:
            self.http.post("/alerts", body=event_dict, target=self.target)
            return True
        except Exception as exc:  # noqa: BLE001 - best-effort fan-out
            logger.debug("degradation alert to S10 failed: %s", exc)
            return False
