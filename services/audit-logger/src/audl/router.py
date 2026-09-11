"""audit_logger — HTTP router (same minimal pattern as S1–S11 Python services).

Routes are registered **literal-before-parameter**: ``/events/verify-chain``
and ``/events/export`` are added before ``/events/{event_id}`` so the literal
segments win (CONVENTIONS §3, same ordering rule S6 uses for
``/positions/snapshot`` vs ``/positions/{symbol}``).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import AUDError, AUDNoRouteError, error_envelope

Handler = Callable[..., Tuple[int, Dict[str, Any]]]


class Route:
    """One (method, path-pattern) binding; ``{param}`` segments match ``[^/]+``."""

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
    """Ordered first-match router over (method, path)."""

    def __init__(self) -> None:
        self._routes: List[Route] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append(Route(method, pattern, handler))

    def dispatch(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        for route in self._routes:
            params = route.match(method, path)
            if params is not None:
                try:
                    return route.handler(**params, _query=query, _body=body)
                except TypeError:
                    try:
                        return route.handler(_query=query, _body=body)
                    except AUDError as exc:
                        return exc.http_status, error_envelope(exc)
                except AUDError as exc:
                    # Single mapping point for every service error: the status
                    # comes from the exception's own http_status (not a
                    # hard-coded 500), and the body is the §1.2 envelope.
                    return exc.http_status, error_envelope(exc)
        return 404, error_envelope(AUDNoRouteError(method, path))


def build_router(controller) -> Router:
    """Wire every S13 endpoint to its controller handler."""
    router = Router()

    def healthz(_query=None, _body=None):
        return controller.healthz()

    def readyz(_query=None, _body=None):
        return controller.readyz()

    def post_events(_query=None, _body=None):
        return controller.append_event(_body)

    def list_events(_query=None, _body=None):
        q = _query or {}
        return controller.list_events(
            source=q.get("source"),
            kind=q.get("kind"),
            limit_raw=q.get("limit"),
        )

    def export_events(_query=None, _body=None):
        q = _query or {}
        return controller.export_events(
            source=q.get("source"),
            kind=q.get("kind"),
            limit_raw=q.get("limit"),
        )

    def verify_chain(_query=None, _body=None):
        return controller.verify_chain()

    def get_event(event_id: str, _query=None, _body=None):
        return controller.event_by_id(event_id)

    def stats(_query=None, _body=None):
        return controller.stats()

    # Order matters: literals before the /events/{event_id} parameter route.
    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("POST", "/events", post_events)
    router.add("GET", "/events", list_events)
    router.add("GET", "/events/export", export_events)
    router.add("GET", "/events/verify-chain", verify_chain)
    router.add("GET", "/events/{event_id}", get_event)
    router.add("GET", "/stats", stats)
    return router
