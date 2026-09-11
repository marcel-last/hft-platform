"""config_service — HTTP router (same minimal pattern as S1/S2/S3/S5/S6/S8/S9)."""

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
                "code": "CFG-404",
                "message": f"no route for {method} {path}",
                "service": "config-service",
                "retryable": False,
                "context": {},
            }
        }


def _int_or(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def build_router(controller) -> Router:
    router = Router()

    def healthz(_query=None, _body=None):
        return controller.healthz()

    def readyz(_query=None, _body=None):
        return controller.readyz()

    def configs(_query=None, _body=None):
        return controller.configs()

    def get_config(service: str, _query=None, _body=None):
        q = _query or {}
        return controller.get_config(service, env=q.get("env"))

    def put_config(service: str, _query=None, _body=None):
        return controller.put_config(service, body=_body)

    def reload(_query=None, _body=None):
        return controller.reload()

    def versions(_query=None, _body=None):
        q = _query or {}
        return controller.versions(service=q.get("service"))

    def changes(_query=None, _body=None):
        q = _query or {}
        since = _int_or(q.get("since"), 0)
        limit = _int_or(q.get("limit"), 100)
        return controller.changes(since=since, limit=limit)

    def watch(_query=None, _body=None):
        q = _query or {}
        since = _int_or(q.get("since"), 0)
        timeout_raw = q.get("timeout_ms")
        timeout_ms = None if timeout_raw is None else _int_or(timeout_raw, -1)
        if timeout_ms is not None and timeout_ms < 0:
            timeout_ms = None
        return controller.watch(since=since, timeout_ms=timeout_ms, service=q.get("service"))

    def set_override(_query=None, _body=None):
        return controller.set_override(body=_body)

    def clear_override(_query=None, _body=None):
        return controller.clear_override(body=_body)

    def list_overrides(_query=None, _body=None):
        q = _query or {}
        return controller.list_overrides(service=q.get("service"))

    def flags(_query=None, _body=None):
        return controller.flags()

    def set_flag(name: str, _query=None, _body=None):
        return controller.set_flag(name, body=_body)

    def stats(_query=None, _body=None):
        return controller.stats()

    # Register literal segments before parameterized ones where they overlap.
    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("GET", "/configs", configs)
    router.add("POST", "/reload", reload)
    router.add("GET", "/versions", versions)
    router.add("GET", "/changes/watch", watch)
    router.add("GET", "/changes", changes)
    router.add("POST", "/overrides", set_override)
    router.add("DELETE", "/overrides", clear_override)
    router.add("GET", "/overrides", list_overrides)
    router.add("GET", "/flags", flags)
    router.add("GET", "/stats", stats)
    # Parameterized routes last so the literals above win.
    router.add("PUT", "/config/{service}", put_config)
    router.add("GET", "/config/{service}", get_config)
    router.add("PUT", "/flags/{name}", set_flag)
    return router
