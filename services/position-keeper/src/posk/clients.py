"""position_keeper — upstream HTTP clients.

Talks to two neighbours using only the stdlib (``urllib``):

* :class:`ExecutionClient`  — S4 execution-gateway: poll ``/fills`` for new
  execution reports to apply to the authoritative ledger, plus liveness.
* :class:`BookBuilderClient` — S2 order-book-builder: pull top-of-book views
  to derive reference (mark) prices for position snapshots.

Both share the tiny blocking :class:`_HTTP` helper so the service has zero
third-party dependencies.  Timeouts come from :class:`~posk.config`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .config import CONFIG
from .errors import UpstreamUnreachableError

logger = logging.getLogger("posk.clients")


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


class ExecutionClient:
    """Client for the execution-gateway (S4)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.ingest.execution_gateway_url,
                          CONFIG.ingest.request_timeout_ms)
        self.target = "execution-gateway"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def fills(self, limit: int = 512) -> List[Dict[str, Any]]:
        """Fetch recent fills (newest first) from the execution gateway."""
        resp = self.http.get(f"/fills?limit={limit}", target=self.target)
        return resp.get("fills", [])


class BookBuilderClient:
    """Client for the order-book-builder (S2)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.snapshot.book_builder_url,
                          CONFIG.snapshot.ref_price_timeout_ms)
        self.target = "order-book-builder"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def top_of_book(self, symbol: str) -> List[Dict[str, Any]]:
        """Fetch the current top-of-book for one symbol across all venues."""
        resp = self.http.get(f"/tob/{symbol}", target=self.target)
        return resp.get("tob", [])

    def books(self) -> List[Dict[str, Any]]:
        """List of all maintained books with health + ToB."""
        resp = self.http.get("/books", target=self.target)
        return resp.get("books", [])
