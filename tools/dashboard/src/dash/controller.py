"""Request handlers for the dashboard (CONVENTIONS §6: controller receives
parsed params and returns ``(status_code, body)`` tuples).

Handler signature used throughout:

    handler(query: Dict[str, str], body: bytes, params: Dict[str, str])
        -> Tuple[int, Dict[str, Any]]

Proxy handlers forward to S15 and return the upstream response **verbatim**
(same status, same JSON body). The only handler-local errors are
``UI-205`` (read-only gate) and the client-raised transport/refresh errors.
"""

import json
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from dash.clients import AuthClient, DashStats, GatewayClient
from dash.config import DashConfig, ServiceEndpoint
from dash.errors import ReadOnlyViolationError, UnsupportedParamError
from dash.router import Router


class ProxyRoute:
    """One dashboard route that forwards to the api-gateway."""

    __slots__ = ("dash_path", "method", "gw_builder", "scope", "body_required")

    def __init__(self, method: str, dash_path: str, gw_builder: Callable,
                 scope: str, body_required: bool = False) -> None:
        self.method = method
        self.dash_path = dash_path
        self.gw_builder = gw_builder          # (params) -> gateway path
        self.scope = scope                   # "read" | "write"
        self.body_required = body_required


#: PLAN §1.4 route table. Order matters: ``/api/portfolio/pnl/{symbol}``
#: is registered *after* ``/api/portfolio/pnl``; the router is exact-match so
#: there is no prefix ambiguity either way.
def build_route_table() -> Tuple[ProxyRoute, ...]:
    # gw_builder takes the router's ``params`` dict, not a single value.
    def pnl(p: Dict[str, str]) -> str:
        return "/portfolio/pnl/" + p["symbol"]

    def finalize(p: Dict[str, str]) -> str:
        return "/settlement/finalize/" + p["date"]

    def reports(p: Dict[str, str]) -> str:
        return "/settlement/reports/" + p["date"]

    def runs(p: Dict[str, str]) -> str:
        return "/settlement/runs/" + p["date"]

    return (
        ProxyRoute("GET", "/api/portfolio/pnl",
                   lambda p: "/portfolio/pnl", "read"),
        ProxyRoute("GET", "/api/portfolio/pnl/{symbol}", pnl, "read"),
        ProxyRoute("GET", "/api/portfolio/metrics",
                   lambda p: "/portfolio/metrics", "read"),
        ProxyRoute("GET", "/api/portfolio/var",
                   lambda p: "/portfolio/var", "read"),
        ProxyRoute("GET", "/api/portfolio/attribution",
                   lambda p: "/portfolio/attribution", "read"),
        ProxyRoute("GET", "/api/portfolio/history",
                   lambda p: "/portfolio/history", "read"),
        ProxyRoute("POST", "/api/settlement/settle",
                   lambda p: "/settlement/settle", "write", body_required=True),
        ProxyRoute("POST", "/api/settlement/finalize/{date}", finalize, "write"),
        ProxyRoute("POST", "/api/settlement/ingest",
                   lambda p: "/settlement/ingest", "write", body_required=True),
        ProxyRoute("GET", "/api/settlement/reports/{date}", reports, "read"),
        ProxyRoute("GET", "/api/settlement/runs/{date}", runs, "read"),
        ProxyRoute("GET", "/api/settlement/discrepancies",
                   lambda p: "/settlement/discrepancies", "read"),
    )


def route_handler(route: ProxyRoute, cfg: DashConfig,
                  gateway: GatewayClient) -> Callable[..., Tuple[int, Dict[str, Any]]]:
    """Build the ``handler(query, body, params)`` closure for a :class:`ProxyRoute`."""

    def handler(query: Dict[str, str], body: bytes,
                params: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        if route.scope == "write" and cfg.read_only:
            raise ReadOnlyViolationError(
                "route %s %s requires write scope but the dashboard runs "
                "--read-only" % (route.method, route.dash_path),
                context={"route": route.dash_path, "method": route.method},
            )
        payload: Optional[bytes] = None
        if route.method == "POST":
            payload = body or b""
            if route.body_required and not payload.strip():
                # The gateway would reject an empty body with its own API-2xx
                # envelope, but settle/ingest are meaningless without JSON:
                # answer locally with UI-201 so the UI gets a clean signal.
                from dash.errors import MalformedBodyError
                raise MalformedBodyError(
                    "a JSON body is required for %s" % route.dash_path,
                    context={"route": route.dash_path},
                )
        gw_path = route.gw_builder(params)
        qs = "&".join("%s=%s" % (k, v) for k, v in sorted(query.items()))
        status, raw = gateway.proxy(route.method, gw_path, qs, payload)
        try:
            doc: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            doc = {"raw": raw.decode("utf-8", "replace")}
        return status, doc if isinstance(doc, dict) else {"data": doc}

    return handler


# ---------------------------------------------------------------------------
# Local (non-proxied) endpoints
# ---------------------------------------------------------------------------

def healthz(cfg: DashConfig) -> Tuple[int, Dict[str, Any]]:
    return 200, {"status": "ok", "service": cfg.name, "version": cfg.version}


def readyz(auth: AuthClient) -> Tuple[int, Dict[str, Any]]:
    ready, reason = auth.ready()
    return 200, {"status": "ready" if ready else "not_ready",
                 "reasons": [reason]}


def _probe_one(host: str, svc: ServiceEndpoint,
               timeout_ms: int) -> Dict[str, Any]:
    """Probe one service: /healthz then /readyz. Never raises."""
    from dash.clients import TransportFailure, _default_transport
    out: Dict[str, Any] = {
        "name": svc.name, "port": svc.port, "package": svc.package,
        "live": False, "healthz": "unreachable", "healthz_version": None,
        "ready": None, "ready_reasons": [], "latency_ms": None,
        "error": None,
    }
    started = _now_ns()
    try:
        status, raw = _default_transport(
            "GET", "http://%s:%d/healthz" % (host, svc.port), None, {},
            timeout_ms / 1000.0,
        )
    except TransportFailure as e:
        out["error"] = "unreachable: %s" % e.detail
        return out
    out["live"] = True
    out["latency_ms"] = (_now_ns() - started) // 1_000_000
    if status == 200:
        out["healthz"] = "ok"
        try:
            doc = json.loads(raw.decode("utf-8"))
            if doc.get("status") == "ok":
                out["healthz_version"] = doc.get("version")
        except (ValueError, UnicodeDecodeError):
            out["healthz"] = "error"
    else:
        out["healthz"] = "error"
        out["error"] = "healthz answered %d" % status
    # readyz is best-effort: a dead healthz already marks the service.
    try:
        status2, raw2 = _default_transport(
            "GET", "http://%s:%d/readyz" % (host, svc.port), None, {},
            timeout_ms / 1000.0,
        )
        if status2 == 200:
            doc2 = json.loads(raw2.decode("utf-8"))
            out["ready"] = doc2.get("status")
            out["ready_reasons"] = list(doc2.get("reasons") or [])
    except (TransportFailure, ValueError, UnicodeDecodeError):
        out["ready"] = None
        if out["error"] is None:
            out["error"] = "readyz unavailable"
    return out


def _now_ns() -> int:
    import time
    return time.time_ns()


def api_health(cfg: DashConfig,
               probe: Optional[Callable[[str, ServiceEndpoint, int], Dict[str, Any]]] = None
               ) -> Tuple[int, Dict[str, Any]]:
    """Sweep all 15 ports in parallel (PLAN §1.5). Always answers 200.

    ``probe`` is injectable for unit tests (must never raise; see
    :func:`_probe_one` for the production probe).
    """
    probe_fn = probe or _probe_one
    started = _now_ns()
    services = list(cfg.health.services)
    results: Dict[int, Dict[str, Any]] = {}
    errors: Dict[int, str] = {}
    lock = threading.Lock()

    def worker(svc: ServiceEndpoint) -> None:
        try:
            res = probe_fn(cfg.health.host, svc, cfg.health.probe_timeout_ms)
        except Exception as e:  # defensive: probe must never kill the sweep
            res = {"name": svc.name, "port": svc.port, "package": svc.package,
                   "live": False, "healthz": "unreachable", "healthz_version": None,
                   "ready": None, "ready_reasons": [], "latency_ms": None,
                   "error": "probe crashed: %s" % e}
        with lock:
            results[svc.port] = res

    threads = [threading.Thread(target=worker, args=(s,), daemon=True)
               for s in services]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=(cfg.health.probe_timeout_ms / 1000.0) + 1.0)

    ordered = [results[s.port] for s in services if s.port in results]
    missing = [s for s in services if s.port not in results]
    for s in missing:  # pragma: no cover - join timed out (shouldn't happen)
        ordered.append({"name": s.name, "port": s.port, "package": s.package,
                        "live": False, "healthz": "unreachable",
                        "healthz_version": None, "ready": None, "ready_reasons": [],
                        "latency_ms": None, "error": "probe thread timed out"})

    live = sum(1 for r in ordered if r["live"])
    down = len(ordered) - live
    not_ready = sum(1 for r in ordered if r["live"] and r["ready"] != "ready")
    return 200, {
        "services": ordered,
        "summary": {
            "live": live, "down": down, "not_ready": not_ready,
            "checked": len(ordered),
            "elapsed_ms": (_now_ns() - started) // 1_000_000,
            "read_only": cfg.read_only,
        },
    }


def api_gateway_stats(cfg: DashConfig, gateway: GatewayClient,
                      stats: DashStats) -> Tuple[int, Dict[str, Any]]:
    """Verbatim S15 /stats + the dashboard-local wrapper (PLAN §1.4)."""
    import time
    status, doc = gateway.get_json("/stats")
    if status != 200 or not isinstance(doc, dict):
        # Verbatim passthrough on any non-200 (upstream envelope untouched).
        return status, doc
    return 200, {
        "stats": doc,
        "dashboard": {
            "read_only": cfg.read_only,
            "auth": stats.snapshot(time.time_ns()),
        },
    }


def build_router(cfg: DashConfig, auth: AuthClient, gateway: GatewayClient,
                 stats: DashStats) -> Router:
    """Assemble the full route table for the dashboard (PLAN §1.4 + local)."""
    router = Router()
    router.add("GET", "/healthz", "healthz")
    router.add("GET", "/readyz", "readyz")
    for route in build_route_table():
        router.add(route.method, route.dash_path,
                   route_handler(route, cfg, gateway))
    router.add("GET", "/api/health", "api_health")
    router.add("GET", "/api/gateway-stats", "api_gateway_stats")
    return router


def dispatch_router(router: Router, method: str, path: str,
                    query: Dict[str, str], body: bytes,
                    cfg: DashConfig, auth: AuthClient, gateway: GatewayClient,
                    stats: DashStats) -> Tuple[int, Dict[str, Any]]:
    """Route one request; returns ``(status, body)`` for any outcome.

    ``NotFound``/``MethodNotAllowed`` become UI-404/UI-405 envelopes;
    registered local endpoints are special-cased; proxy routes run their
    closure. Exceptions from any handler become the §1.2 envelope.
    """
    from dash.errors import DashError, error_response
    from dash.router import MethodNotAllowed as _MNLA
    res = router.resolve(method, path)
    if not hasattr(res, "name"):  # NotFound or MethodNotAllowed
        if isinstance(res, _MNLA):
            return 405, {"error": {
                "code": "UI-405", "message": "method %s not allowed on %s"
                % (method, path), "service": "dashboard", "retryable": False,
                "context": {"allowed": list(res.allowed)}}}
        return 404, {"error": {
            "code": "UI-404", "message": "no dashboard route for %s %s"
            % (method, path), "service": "dashboard", "retryable": False,
            "context": {}}}
    name = res.name
    params = res.params or {}
    try:
        if name == "healthz":
            return healthz(cfg)
        if name == "readyz":
            return readyz(auth)
        if name == "api_health":
            return api_health(cfg)
        if name == "api_gateway_stats":
            return api_gateway_stats(cfg, gateway, stats)
        if res.handler is not None:
            return res.handler(query, body, params)
        return 404, {"error": {
            "code": "UI-404", "message": "route %s has no handler" % name,
            "service": "dashboard", "retryable": False, "context": {}}}
    except DashError as e:
        return error_response(e)
    except Exception as e:  # defensive: never leak a stack trace to the wire
        from dash.errors import InternalError
        return error_response(InternalError(
            "unexpected internal error: %s" % e,
            context={"exception": type(e).__name__}))
