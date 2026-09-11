"""portfolio_analytics — HTTP router (same minimal pattern as S1/S2/S3/S5/S6/S8)."""

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
                "code": "PFA-404",
                "message": f"no route for {method} {path}",
                "service": "portfolio-analytics",
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

    def pnl(_query=None, _body=None):
        return controller.pnl()

    def pnl_symbol(symbol: str, _query=None, _body=None):
        return controller.pnl_symbol(symbol)

    def metrics(_query=None, _body=None):
        return controller.metrics()

    def attribution(_query=None, _body=None):
        return controller.attribution()

    def var(_query=None, _body=None):
        q = _query or {}
        method = q.get("method")
        confidence_raw = q.get("confidence")
        confidence: Optional[float] = None
        if confidence_raw is not None:
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                confidence = None
        return controller.var(method=method, confidence=confidence)

    def history(_query=None, _body=None):
        q = _query or {}
        try:
            limit = int(q.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        return controller.history(limit=limit)

    def stats(_query=None, _body=None):
        return controller.stats()

    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("GET", "/pnl", pnl)
    router.add("GET", "/metrics", metrics)
    router.add("GET", "/attribution", attribution)
    router.add("GET", "/var", var)
    router.add("GET", "/history", history)
    router.add("GET", "/stats", stats)
    # /pnl/{symbol} is registered last so the literal segments above win.
    router.add("GET", "/pnl/{symbol}", pnl_symbol)
    return router
