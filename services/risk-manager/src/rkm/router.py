"""risk_manager — HTTP router (same minimal pattern as S1/S2/S3)."""

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
                "code": "RKM-404",
                "message": f"no route for {method} {path}",
                "service": "risk-manager",
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

    def pre_trade_check(_query=None, _body=None):
        return controller.pre_trade_check(_body)

    def get_limits(_query=None, _body=None):
        return controller.get_limits()

    def put_limits(_query=None, _body=None):
        return controller.put_limits(_body)

    def reset_limits(_query=None, _body=None):
        return controller.reset_limits()

    def exposure(_query=None, _body=None):
        return controller.exposure()

    def engage_kill_switch(_query=None, _body=None):
        return controller.engage_kill_switch(_body)

    def disengage_kill_switch(_query=None, _body=None):
        return controller.disengage_kill_switch()

    def kill_switch_status(_query=None, _body=None):
        return controller.kill_switch_status()

    def breaches(_query=None, _body=None):
        q = _query or {}
        limit = int(q.get("limit", "100"))
        severity = q.get("severity")
        return controller.breaches(limit=limit, severity=severity)

    def stats(_query=None, _body=None):
        return controller.stats()

    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("POST", "/pre-trade-check", pre_trade_check)
    router.add("GET", "/limits", get_limits)
    router.add("PUT", "/limits", put_limits)
    router.add("DELETE", "/limits", reset_limits)
    router.add("GET", "/exposure", exposure)
    router.add("POST", "/kill-switch", engage_kill_switch)
    router.add("POST", "/kill-switch/disengage", disengage_kill_switch)
    router.add("GET", "/kill-switch", kill_switch_status)
    router.add("GET", "/breaches", breaches)
    router.add("GET", "/stats", stats)
    return router
