"""audit_logger — service entrypoint.

Boot sequence:

1.  Parse CLI arguments (``--bind`` / ``--port``), then validate configuration
    (abort with a structured error on failure, CONVENTIONS §5).
2.  Build the :class:`AuditEngine` with a production :class:`SystemClock` and
    wire the controller + router.
3.  Start the HTTP API thread (``ThreadingHTTPServer``, CONVENTIONS §3).
4.  Block until SIGINT / SIGTERM, then shut down cleanly.

S13 is a pure request/response service — there is no background ingest loop;
events arrive via ``POST /events`` from S4 (order lifecycle), S5 (risk vetoes +
kill-switch activations) and S11 (config changes).

Run with::

    python -m audl.main
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .audit_engine import AuditEngine
from .config import AuditConfig, validate_config
from .controller import AuditController
from .errors import AUDBootError, error_envelope
from .models import SystemClock
from .router import build_router

logger = logging.getLogger("audl.main")


def load_config(args: argparse.Namespace) -> AuditConfig:
    """Build the service config from defaults + CLI overrides, validating once."""
    cfg = AuditConfig()
    server = cfg.server
    if args.bind:
        server = dataclasses.replace(server, bind_host=args.bind)
    if args.port is not None:
        server = dataclasses.replace(server, listen_port=args.port)
    if args.bind or args.port is not None:
        cfg = dataclasses.replace(cfg, server=server)
    errors = validate_config(cfg)
    if errors:
        raise AUDBootError("configuration validation failed", context={"errors": errors})
    return cfg


class _HTTPHandler(BaseHTTPRequestHandler):
    server_version = "AUDL/1.0"
    router = None
    controller = None

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            # Bad JSON is a protocol error, not a 500: report AUD-001.
            from .errors import AUDProtocolError

            self._send_json(
                400,
                error_envelope(AUDProtocolError("request body is not valid JSON")),
            )
            return "__handled__"  # type: ignore[return-value]

    def _handle(self, method: str, body: Optional[dict] = None) -> None:
        if body == "__handled__":  # type: ignore[comparison-overlap]
            return
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            status, resp_body = self.router.dispatch(method, parsed.path, query, body=body)
        except Exception as exc:  # pragma: no cover - defensive net
            logger.error("unhandled error on %s %s: %r", method, parsed.path, exc)
            status, resp_body = 500, error_envelope(exc)
        self._send_json(status, resp_body)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST", body=self._read_body())

    def do_DELETE(self) -> None:  # noqa: N802
        # S13 has no DELETE routes; the router answers AUD-404 (no route) and
        # this handler exists so the stdlib server does not pre-empt it with
        # its own 501 page.
        self._handle("DELETE")


def serve_http(router, controller, host: str, port: int) -> ThreadingHTTPServer:
    _HTTPHandler.router = router
    _HTTPHandler.controller = controller
    httpd = ThreadingHTTPServer((host, port), _HTTPHandler)
    threading.Thread(target=httpd.serve_forever, name="audl-http", daemon=True).start()
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="audl.main", description="audit-logger (S13)")
    parser.add_argument("--bind", default=None, help="bind host (default from config)")
    parser.add_argument("--port", type=int, default=None, help="listen port (default 7730)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    try:
        cfg = load_config(args)
    except AUDBootError as exc:
        print(json.dumps(error_envelope(exc)), file=sys.stderr)
        return 1

    engine = AuditEngine(cfg, clock=SystemClock())
    controller = AuditController(cfg=cfg, engine=engine)
    router = build_router(controller)

    try:
        httpd = serve_http(router, controller, cfg.server.bind_host, cfg.server.listen_port)
    except OSError as exc:
        print(
            json.dumps(
                error_envelope(
                    AUDBootError(
                        f"cannot bind {cfg.server.bind_host}:{cfg.server.listen_port}",
                        context={"detail": str(exc)},
                    )
                )
            ),
            file=sys.stderr,
        )
        return 1

    logger.info(
        "audit-logger v%s starting on %s:%d (env=%s)",
        cfg.version, cfg.server.bind_host, cfg.server.listen_port, cfg.env,
    )

    stop = threading.Event()

    def _sig(signum, frame):  # noqa: ARG001 - signal handler signature
        logger.info("signal %s; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    try:
        while not stop.is_set():
            stop.wait(0.25)
    finally:
        httpd.shutdown()
    logger.info("shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
