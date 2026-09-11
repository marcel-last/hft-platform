"""position_keeper — HTTP router (same minimal pattern as S1/S2/S3/S5)."""

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
                "code": "POS-404",
                "message": f"no route for {method} {path}",
                "service": "position-keeper",
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

    def positions(_query=None, _body=None):
        q = _query or {}
        account = q.get("account")
        include_flat = str(q.get("include_flat", "false")).lower() in ("1", "true", "yes")
        return controller.positions(account=account, include_flat=include_flat)

    def snapshot(_query=None, _body=None):
        return controller.snapshot()

    def position_symbol(symbol: str, _query=None, _body=None):
        q = _query or {}
        account = q.get("account")
        return controller.position_by_symbol(symbol, account=account)

    def adjust(_query=None, _body=None):
        return controller.adjust(_body)

    def corporate_action(_query=None, _body=None):
        return controller.corporate_action(_body)

    def history(symbol: str, _query=None, _body=None):
        q = _query or {}
        try:
            limit = int(q.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        return controller.history(symbol, limit=limit)

    def stats(_query=None, _body=None):
        return controller.stats()

    # NOTE: /positions/snapshot must be registered BEFORE /positions/{symbol}
    # so the literal segment wins over the parameterized route.
    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("GET", "/positions", positions)
    router.add("GET", "/positions/snapshot", snapshot)
    router.add("POST", "/adjust", adjust)
    router.add("POST", "/corporate-action", corporate_action)
    router.add("GET", "/history/{symbol}", history)
    router.add("GET", "/positions/{symbol}", position_symbol)
    router.add("GET", "/stats", stats)
    return router
