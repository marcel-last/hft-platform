"""data_quality_monitor — HTTP router (same minimal pattern as S1/S2/S3/S5/S6)."""

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

    def dispatch(self, method: str, path: str, query: Dict[str, str],
                 body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        for route in self._routes:
            params = route.match(method, path)
            if params is not None:
                try:
                    return route.handler(**params, _query=query, _body=body)
                except TypeError:
                    return route.handler(_query=query, _body=body)
        return 404, {
            "error": {
                "code": "DQM-404",
                "message": f"no route for {method} {path}",
                "service": "data-quality-monitor",
                "retryable": False,
                "context": {},
            }
        }


def build_router(controller) -> Router:
    router = Router()

    def healthz(_query=None, _body=None):
        return controller.healthz()

    def readyz(_query=None, _body=None):
        return controller.readyz()

    def quality_summary(_query=None, _body=None):
        return controller.quality_summary()

    def quality_symbol(symbol: str, _query=None, _body=None):
        return controller.quality_symbol(symbol)

    def gaps(_query=None, _body=None):
        q = _query or {}
        try:
            limit = int(q.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        return controller.gaps(limit=limit)

    def staleness(_query=None, _body=None):
        q = _query or {}
        try:
            limit = int(q.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        return controller.staleness(limit=limit)

    def degradations(_query=None, _body=None):
        q = _query or {}
        try:
            limit = int(q.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        kind = q.get("kind")
        return controller.degradations(limit=limit, kind=kind)

    def stats(_query=None, _body=None):
        return controller.stats()

    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("GET", "/quality-summary", quality_summary)
    router.add("GET", "/gaps", gaps)
    router.add("GET", "/staleness", staleness)
    router.add("GET", "/degradations", degradations)
    router.add("GET", "/stats", stats)
    # /quality/{symbol} is registered last so the literal segments above win.
    router.add("GET", "/quality/{symbol}", quality_symbol)
    return router
