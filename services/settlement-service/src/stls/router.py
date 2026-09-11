"""settlement_service — HTTP router (same minimal pattern as the S1–S13 Python services).

Routes are registered **literal-before-parameter** where a literal can collide
with a parameter: none of S14's parameter routes overlap a literal sibling, so
ordering is simply by construction.  The router is also the **single mapping
point** from :class:`STLError` to ``(http_status, error_envelope)`` — handler-
raised errors must never fall through to the 500 net (lesson learned in S13).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import STLError, NoRouteError, error_envelope, make_error

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
        allowed: List[str] = []
        for r in self._routes:
            if r.method != method.upper() and r._regex.match(path) is not None:
                allowed.append(r.method)
        for route in self._routes:
            params = route.match(method, path)
            if params is not None:
                try:
                    return route.handler(**params, _query=query, _body=body)
                except TypeError:
                    try:
                        return route.handler(_query=query, _body=body)
                    except STLError as exc:
                        return exc.http_status, error_envelope(exc)
                except STLError as exc:
                    return exc.http_status, error_envelope(exc)
        if allowed:
            return 405, error_envelope(make_error(
                "STL-405", "The method is not allowed for this path.",
                status=405, context={"allowed": sorted(set(allowed)), "method": method, "path": path}))
        return 404, error_envelope(NoRouteError(context={"method": method, "path": path}))

def build_router(controller) -> Router:
    """Wire every S14 endpoint to its controller handler."""
    router = Router()

    def healthz(_query=None, _body=None):
        return controller.healthz()

    def readyz(_query=None, _body=None):
        return controller.readyz()

    def post_settle(_query=None, _body=None):
        return controller.settle(_body)

    def finalize_date(date: str, _query=None, _body=None):
        return controller.finalize(date)

    def get_report(date: str, _query=None, _body=None):
        return controller.report(date)

    def get_run(date: str, _query=None, _body=None):
        return controller.run_status(date)

    def get_discrepancies(_query=None, _body=None):
        q = _query or {}
        return controller.discrepancies(
            date=q.get("date"), venue=q.get("venue"),
            kind=q.get("kind"), limit_raw=q.get("limit"))

    def post_ingest(_query=None, _body=None):
        return controller.ingest_positions(_body)

    def stats(_query=None, _body=None):
        return controller.stats()

    router.add("GET", "/healthz", healthz)
    router.add("GET", "/readyz", readyz)
    router.add("POST", "/settle", post_settle)
    router.add("POST", "/finalize/{date}", finalize_date)
    router.add("GET", "/reports/{date}", get_report)
    router.add("GET", "/runs/{date}", get_run)
    router.add("GET", "/discrepancies", get_discrepancies)
    router.add("POST", "/ingest", post_ingest)
    router.add("GET", "/stats", stats)
    return router
