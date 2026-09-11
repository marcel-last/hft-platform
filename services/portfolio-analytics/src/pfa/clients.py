"""portfolio_analytics — upstream HTTP clients.

Talks to a single neighbour using only the stdlib (``urllib``):

* :class:`PositionKeeperClient` — S6 position-keeper: pull ``/positions`` for
  the authoritative position book (realized P&L + mark-to-market values) on
  every refresh pass.

The tiny blocking :class:`_HTTP` helper is shared so the service has zero
third-party dependencies.  Timeouts come from :class:`~pfa.config`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .config import CONFIG
from .errors import UpstreamUnreachableError

logger = logging.getLogger("pfa.clients")


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


class PositionKeeperClient:
    """Client for the position-keeper (S6)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.ingest.position_keeper_url,
                          CONFIG.ingest.request_timeout_ms)
        self.target = "position-keeper"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def positions(self, account: Optional[str] = None,
                  include_flat: bool = True) -> List[Dict[str, Any]]:
        """Fetch the authoritative position book (``GET /positions``)."""
        params = []
        if account:
            params.append(f"account={urllib.parse.quote(account)}")
        if not include_flat:
            params.append("include_flat=false")
        query = ("?" + "&".join(params)) if params else ""
        resp = self.http.get(f"/positions{query}", target=self.target)
        return resp.get("positions", [])
