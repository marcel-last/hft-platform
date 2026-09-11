"""market_data_gateway — HTTP router.

A deliberately small, dependency-free router: a table of (method, pattern) ->
handler, with ``{param}`` path segments.  It returns ``(status_code, body_dict)``
tuples; the transport layer in ``main.py`` serializes to JSON.

Keeping the router free of any web framework keeps the service's dependency
surface tiny and makes the hot distribution path (which does *not* go through
this router at all — subscribers receive quotes over the internal push
channel) completely independent of request handling.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

Handler = Callable[..., Tuple[int, Dict[str, Any]]]


class Route:
    """One compiled route entry."""

    def __init__(self, method: str, pattern: str, handler: Handler) -> None:
        self.method = method.upper()
        # convert "/quotes/{symbol}" into a regex with named groups
        parts = pattern.split("/")
        regex_parts: List[str] = []
        for part in parts:
            if part.startswith("{") and part.endswith("}"):
                regex_parts.append(f"(?P<{part[1:-1]}>[^/]+)")
            else:
                regex_parts.append(re.escape(part))
        self._regex = re.compile("^" + "/".join(regex_parts) + "$")
        self.handler = handler

    def match(self, method: str, path: str) -> Optional[Dict[str, str]]:
        if self.method != method.upper():
            return None
        m = self._regex.match(path)
        if m is None:
            return None
        return m.groupdict()


class Router:
    """Method+path router with a small route table."""

    def __init__(self) -> None:
        self._routes: List[Route] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append(Route(method, pattern, handler))

    def dispatch(self, method: str, path: str, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        for route in self._routes:
            params = route.match(method, path)
            if params is not None:
                try:
                    return route.handler(**params, _query=query)
                except TypeError:
                    # handler does not accept keyword params; call bare
                    return route.handler(_query=query)
        return 404, {
            "error": {
                "code": "MDG-404",
                "message": f"no route for {method} {path}",
                "service": "market-data-gateway",
                "retryable": False,
                "context": {},
            }
        }

    def table(self) -> List[Dict[str, str]]:
        return [{"method": r.method, "pattern": r._regex.pattern} for r in self._routes]


def build_router(controller) -> Router:
    """Wire the gateway controller's handlers into a fresh router."""
    router = Router()

    def healthz(_query=None):
        return controller.healthz()

    def readyz(_query=None):
        return controller.readyz()

    def feeds(_query=None):
        return controller.feeds()

    def symbols(_query=None):
        return controller.symbols()

    def quotes(symbol: str, _query=None):
        limit = int((_query or {}).get("limit", "50"))
        return controller.latest_quotes(symbol, limit=limit)

    def quote_history(symbol: str, _query=None):
        limit = int((_query or {}).get("limit", "100"))
        return controller.quote_history(symbol, limit=limit)

    def quality(_query=None):
        return controller.quality_report()

    def resync(_query=None):
        return controller.resync()

    def subscribers(_query=None):
        return controller.subscribers()

    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("GET", "/feeds", feeds)
    router.add("GET", "/symbols", symbols)
    router.add("GET", "/quotes/{symbol}", quotes)
    router.add("GET", "/history/{symbol}", quote_history)
    router.add("GET", "/quality", quality)
    router.add("POST", "/resync", resync)
    router.add("GET", "/subscribers", subscribers)
    return router
