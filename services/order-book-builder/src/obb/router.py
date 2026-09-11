"""order_book_builder — HTTP router (same minimal pattern as S1)."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

Handler = Callable[..., Tuple[int, Dict[str, Any]]]


class Route:
    def __init__(self, method: str, pattern: str, handler: Handler) -> None:
        self.method = method.upper()
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
        return m.groupdict() if m else None


class Router:
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
                    return route.handler(_query=query)
        return 404, {
            "error": {
                "code": "OBB-404",
                "message": f"no route for {method} {path}",
                "service": "order-book-builder",
                "retryable": False,
                "context": {},
            }
        }


def build_router(controller) -> Router:
    router = Router()

    def healthz(_query=None):
        return controller.healthz()

    def readyz(_query=None):
        return controller.readyz()

    def books(_query=None):
        return controller.books()

    def book(symbol: str, _query=None):
        return controller.book_snapshot(symbol)

    def tob(symbol: str, _query=None):
        return controller.top_of_book(symbol)

    def events(_query=None):
        limit = int((_query or {}).get("limit", "100"))
        return controller.events(limit=limit)

    def rebuild(symbol: str, _query=None):
        return controller.rebuild(symbol)

    def stats(_query=None):
        return controller.stats()

    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("GET", "/books", books)
    router.add("GET", "/book/{symbol}", book)
    router.add("GET", "/tob/{symbol}", tob)
    router.add("GET", "/events", events)
    router.add("POST", "/rebuild/{symbol}", rebuild)
    router.add("GET", "/stats", stats)
    return router
