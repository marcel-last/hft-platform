"""strategy_engine — HTTP router (same minimal pattern as S1/S2)."""

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
                "code": "STE-404",
                "message": f"no route for {method} {path}",
                "service": "strategy-engine",
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

    def signals(_query=None, _body=None):
        q = _query or {}
        limit = int(q.get("limit", "100"))
        return controller.signals(symbol=q.get("symbol", ""), limit=limit)

    def signal_id(signal_id: str, _query=None, _body=None):
        return controller.signal_by_id(signal_id)

    def strategies(_query=None, _body=None):
        return controller.strategies()

    def pause_strategy(_query=None, _body=None):
        q = _query or {}
        body = dict(_body or {})
        if "id" not in body and "id" in q:
            body["id"] = q["id"]
        return controller.pause_strategy(body)

    def resume_strategy(_query=None, _body=None):
        q = _query or {}
        body = dict(_body or {})
        if "id" not in body and "id" in q:
            body["id"] = q["id"]
        return controller.resume_strategy(body)

    def pause_all(_query=None, _body=None):
        return controller.pause_all()

    def resume_all(_query=None, _body=None):
        return controller.resume_all()

    def intents(_query=None, _body=None):
        limit = int((_query or {}).get("limit", "100"))
        return controller.intents(limit=limit)

    def stats(_query=None, _body=None):
        return controller.stats()

    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("GET", "/signals", signals)
    router.add("GET", "/signals/{signal_id}", signal_id)
    router.add("GET", "/strategies", strategies)
    router.add("POST", "/strategies/pause", pause_strategy)
    router.add("POST", "/strategies/resume", resume_strategy)
    router.add("POST", "/pause", pause_all)
    router.add("POST", "/resume", resume_all)
    router.add("GET", "/intents", intents)
    router.add("GET", "/stats", stats)
    return router
