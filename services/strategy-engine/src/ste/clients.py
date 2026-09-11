"""strategy_engine — upstream/downstream HTTP clients.

Talks to three neighbours using only the stdlib (``urllib``):

* :class:`GatewayClient`  — S1 market-data-gateway: poll normalized quotes and
  read the symbol table (tick sizes).
* :class:`BookBuilderClient` — S2 order-book-builder: poll top-of-book views,
  book health, and material book events.
* :class:`ExecutionClient` — S4 execution-gateway: push order intents and check
  their acknowledgment status.

All three share the tiny blocking :class:`_HTTP` helper so the service has zero
third-party dependencies.  Timeouts come from :class:`~ste.config`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .config import CONFIG
from .errors import ExecutionGatewayError, UpstreamUnreachableError
from .models import BookView

logger = logging.getLogger("ste.clients")


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
            if target == "execution-gateway":
                raise ExecutionGatewayError(
                    f"execution gateway returned {exc.code} for {method} {path}",
                    context={"status": exc.code, "body": parsed},
                ) from exc
            raise UpstreamUnreachableError(
                target, f"{exc.code} on {method} {path}", context={"status": exc.code, "body": parsed},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if target == "execution-gateway":
                raise ExecutionGatewayError(
                    f"cannot reach execution gateway at {url}: {exc}", context={"url": url},
                ) from exc
            raise UpstreamUnreachableError(target, str(exc), context={"url": url}) from exc

    def get(self, path: str, target: str = "upstream") -> Dict[str, Any]:
        return self._request("GET", path, target=target)

    def post(self, path: str, body: Optional[dict] = None,
             target: str = "upstream") -> Dict[str, Any]:
        return self._request("POST", path, body, target=target)


class GatewayClient:
    """Client for the market-data-gateway (S1)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.ingest.gateway_url, CONFIG.ingest.request_timeout_ms)
        self.target = "market-data-gateway"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def poll_quotes(self, symbol: str, limit: int = 256) -> List[dict]:
        """Non-consuming view of the most recent normalized quotes for a symbol."""
        resp = self.http.get(f"/history/{symbol}?limit={limit}", target=self.target)
        return resp.get("quotes", [])

    def latest_quotes(self, symbol: str, limit: int = 256) -> List[dict]:
        """Consuming view of the gateway's per-symbol quote buffer."""
        resp = self.http.get(f"/quotes/{symbol}?limit={limit}", target=self.target)
        return resp.get("quotes", [])

    def symbols(self) -> List[Dict[str, Any]]:
        """Symbol table with tick sizes (used to seed the engine's tick map)."""
        resp = self.http.get("/symbols", target=self.target)
        return resp.get("symbols", [])


class BookBuilderClient:
    """Client for the order-book-builder (S2)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.ingest.book_builder_url, CONFIG.ingest.request_timeout_ms)
        self.target = "order-book-builder"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def top_of_book(self, symbol: str) -> List[BookView]:
        """Fetch the current top-of-book for one symbol across all venues."""
        resp = self.http.get(f"/tob/{symbol}", target=self.target)
        views: List[BookView] = []
        for entry in resp.get("tob", []):
            venue = entry.get("venue", "primary")
            try:
                views.append(BookView.from_tob_dict(entry, symbol, venue, tick_size=0.25))
            except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover - defensive
                logger.debug("skipping malformed ToB entry for %s/%s: %s", symbol, venue, exc)
        return views

    def books(self) -> List[Dict[str, Any]]:
        """List of all maintained books with health + ToB."""
        resp = self.http.get("/books", target=self.target)
        return resp.get("books", [])

    def events(self, limit: int = 512) -> List[Dict[str, Any]]:
        """Recent material book events (TOP_CHANGE, SPREAD_CHANGE, ...)."""
        resp = self.http.get(f"/events?limit={limit}", target=self.target)
        return resp.get("events", [])


class ExecutionClient:
    """Client for the execution-gateway (S4)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.emit.execution_gateway_url, CONFIG.emit.request_timeout_ms)
        self.target = "execution-gateway"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def push_intent(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Submit one order intent to the execution gateway."""
        return self.http.post("/orders", body=intent, target=self.target)

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self.http.get(f"/orders/{order_id}", target=self.target)
