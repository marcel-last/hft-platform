"""Entrypoint for the dashboard tool (CONVENTIONS §6: parse args, validate
config once, serve).

Run (from ``tools/dashboard``):

    PYTHONPATH=src python3 -m dash.main --bind 127.0.0.1 --port 7760 \
        --auth-url http://127.0.0.1:7720 --gateway-url http://127.0.0.1:7750
"""

import argparse
import json
import logging
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from dash import __version__
from dash.clients import AuthClient, DashStats, GatewayClient
from dash.config import (
    SERVICE_NAME,
    AuthConfig,
    DashConfig,
    GatewayConfig,
    HealthConfig,
    validate_config,
)
from dash.controller import build_router, dispatch_router
from dash.errors import DashError, UnknownRouteError
from dash.router import Router


LOGGER_NAME = "dash"


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dash.main",
        description="HFT platform operations dashboard (tool, not a service).",
    )
    p.add_argument("--bind", default="0.0.0.0", help="listen address")
    p.add_argument("--port", type=int, default=7760, help="listen port")
    p.add_argument("--auth-url", default="http://127.0.0.1:7720",
                   help="S12 auth-service base URL")
    p.add_argument("--gateway-url", default="http://127.0.0.1:7750",
                   help="S15 api-gateway base URL")
    p.add_argument("--read-only", action="store_true",
                   help="refuse write-scope routes with 403 UI-205")
    return p.parse_args(argv)


def build_config(args: argparse.Namespace) -> DashConfig:
    return DashConfig(
        bind=args.bind,
        port=args.port,
        read_only=args.read_only,
        auth=AuthConfig(base_url=args.auth_url.rstrip("/")),
        gateway=GatewayConfig(base_url=args.gateway_url.rstrip("/")),
        health=HealthConfig(),
    )


class _Handler(BaseHTTPRequestHandler):
    """One handler per request; JSON only (CONVENTIONS §3)."""

    server_version = "dash/" + __version__  # type: ignore[attr-defined]
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        logging.getLogger(LOGGER_NAME).info(fmt, *args)

    def _send_bytes(self, status: int, body: bytes,
                    content_type: str) -> None:
        """M3: raw-bytes response (static assets) with no-cache.

        Mirrors ``_send`` (JSON) but emits arbitrary bytes; the UI's
        assets must never be cached (PLAN §4.1: ``no-cache``), so the
        header is set unconditionally.
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; nothing to do

    def _serve_index(self) -> None:
        """M3: ``GET /`` serves the single-page UI (PLAN §4.1)."""
        from dash import staticfiles

        try:
            body, ctype = staticfiles.read_static_file("index.html")
        except staticfiles.StaticFileError as e:
            self._send(404, staticfiles.ui404_envelope("/"))
            return
        self._send_bytes(200, body, ctype)

    def _serve_static(self, path: str) -> None:
        """M3: ``GET /static/<file>`` (PLAN §4.1).

        Only the three whitelisted files resolve; everything else —
        including traversal attempts — is a 404 UI-404 JSON envelope,
        never a raw filesystem disclosure.
        """
        from dash import staticfiles

        name = path[len("/static/"):].split("?", 1)[0]
        # Reject any residual directory component before the whitelist;
        # the name handed down must be a bare filename.
        if "/" in name or name in ("", ".", ".."):
            self._send(404, staticfiles.ui404_envelope(path))
            return
        try:
            body, ctype = staticfiles.read_static_file(name)
        except staticfiles.StaticFileError:
            self._send(404, staticfiles.ui404_envelope(path))
            return
        self._send_bytes(200, body, ctype)

    def _serve_live(self) -> None:
        """M2: stream ``GET /live/stream`` on the request thread (PLAN §3.4).

        Query parsing keeps the raw multi-value lists (``?symbol=`` is
        repeatable, PLAN §3.3); ``parse_live_query`` raises ``UI-202``
        *before* the stream opens (PLAN §3.5).
        """
        from dash.live import LiveConfig, parse_live_query, serve_stream

        cfg: DashConfig = self.server.dash_config  # type: ignore[attr-defined]
        try:
            request = parse_live_query(
                {k: v for k, v in urllib.parse.parse_qs(self.query_string).items()},
                LiveConfig())
        except DashError as e:
            from dash.errors import error_response
            status, body = error_response(e)
            self._send(status, body)
            return
        live_cfg = LiveConfig(host="127.0.0.1")
        serve_stream(self, request, live_cfg)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send(self, status: int, body: Dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; nothing to do

    def _dispatch(self, method: str) -> None:
        try:
            cfg = self.server.dash_config  # type: ignore[attr-defined]
            router: Router = self.server.dash_router  # type: ignore[attr-defined]
            gateway: GatewayClient = self.server.dash_gateway  # type: ignore[attr-defined]
            auth: AuthClient = self.server.dash_auth  # type: ignore[attr-defined]
            stats: DashStats = self.server.dash_stats  # type: ignore[attr-defined]

            path, _, query_str = self.path.partition("?")
            self.query_string = query_str  # raw query, for /live/*
            # M3: the UI is served by the dashboard itself (no gateway).
            # ``/`` is the page; ``/static/...`` its two companion files.
            # Both are GET-only and are handled before the router so a
            # non-GET on them falls through to UI-405/404 like any other
            # known-but-unmatched route (M1 behaviour preserved).
            if method == "GET" and (path == "/" or path == ""):
                self._serve_index()
                return
            if method == "GET" and (path == "/static" or
                                    path.startswith("/static/")):
                self._serve_static(path)
                return
            query: Dict[str, str] = {}
            if query_str:
                for k, v in urllib.parse.parse_qs(query_str).items():
                    query[k] = v[0] if v else ""
            if method == "GET" and path.rstrip("/") == "/live/stream":
                self._serve_live()
                return
            body = self._read_body() if method in ("POST", "PUT") else b""
            status, doc = dispatch_router(
                router, method, path, query, body, cfg, auth, gateway, stats)
            self._send(status, doc)
        except UnknownRouteError:
            self._send(404, {"error": {"code": "UI-404",
                                       "message": "no dashboard route",
                                       "service": SERVICE_NAME,
                                       "retryable": False, "context": {}}})
        except Exception as e:  # last-resort: envelope, never a stack trace
            logging.getLogger(LOGGER_NAME).error(
                "unhandled error on %s %s: %s", method, self.path, e)
            self._send(500, {"error": {"code": "UI-001",
                                       "message": "unexpected internal error: %s" % e,
                                       "service": SERVICE_NAME,
                                       "retryable": False,
                                       "context": {"exception": type(e).__name__}}})

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")


def serve(cfg: DashConfig, router: Router, auth: AuthClient,
          gateway: GatewayClient, stats: DashStats) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((cfg.bind, cfg.port), _Handler)
    httpd.dash_config = cfg  # type: ignore[attr-defined]
    httpd.dash_router = router  # type: ignore[attr-defined]
    httpd.dash_gateway = gateway  # type: ignore[attr-defined]
    httpd.dash_auth = auth  # type: ignore[attr-defined]
    httpd.dash_stats = stats  # type: ignore[attr-defined]
    return httpd


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger(LOGGER_NAME)
    args = parse_args(argv)
    cfg = build_config(args)

    errors = validate_config(cfg)
    if errors:
        for e in errors:
            log.error("config error: %s", e)
        return 1

    stats = DashStats()
    auth = AuthClient(cfg.auth, stats=stats)
    gateway = GatewayClient(cfg.gateway, auth)
    router = build_router(cfg, auth, gateway, stats)

    # Acquire the first token eagerly so /readyz is meaningful immediately;
    # a failure is NOT fatal — the dashboard still serves /healthz, /api/health
    # and will keep retrying per-request.
    try:
        auth.get_token()
        log.info("token acquired (jti=%s)", auth.current_jti())
    except Exception as e:
        log.warning("initial token acquisition failed: %s (will retry per request)", e)

    try:
        httpd = serve(cfg, router, auth, gateway, stats)
    except OSError as e:
        log.error("cannot bind %s:%d: %s", cfg.bind, cfg.port, e)
        return 1
    print("%s v%s starting on %s:%d (auth=%s gateway=%s read_only=%s)"
          % (SERVICE_NAME, __version__, cfg.bind, cfg.port,
             cfg.auth.base_url, cfg.gateway.base_url, cfg.read_only))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down (keyboard interrupt)")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
