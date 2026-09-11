"""settlement_service — service entrypoint.

Boot sequence:

1.  Parse CLI arguments (``--bind`` / ``--port``), then validate configuration
    (abort with a structured error on failure, CONVENTIONS §5).
2.  Build the :class:`SettlementEngine` with a production :class:`SystemClock`,
    wire the ingest client (S6) and the controller + router.
3.  Start the HTTP API thread (``ThreadingHTTPServer``, CONVENTIONS §3).
4.  Optionally start the background ingest loop (S6 position snapshots into
    today's run).
5.  Block until SIGINT / SIGTERM, then shut down cleanly.

Run with::

    python -m stls.main
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
from typing import List, Optional

from .clients import PositionKeeperClient
from .config import SettlementConfig, validate_config
from .controller import SettlementController
from .errors import STLError, make_error, error_envelope
from .models import SystemClock
from .router import build_router
from .settlement_engine import SettlementEngine

logger = logging.getLogger("stls.main")


def load_config(args: argparse.Namespace) -> SettlementConfig:
    """Build the service config from defaults + CLI overrides, validating once."""
    cfg = SettlementConfig()
    srv = cfg.server
    if args.bind:
        srv = dataclasses.replace(srv, host=args.bind)
    if args.port is not None:
        srv = dataclasses.replace(srv, port=args.port)
    if args.bind or args.port is not None:
        cfg = dataclasses.replace(cfg, server=srv)
    if args.positions_url:
        cfg = dataclasses.replace(
            cfg, ingest=dataclasses.replace(cfg.ingest, position_keeper_url=args.positions_url))
    errors = validate_config(cfg)
    if errors:
        raise make_error("STL-101", "configuration validation failed",
                         status=500, context={"errors": errors})
    return cfg


class _HTTPHandler(BaseHTTPRequestHandler):
    server_version = "STLS/1.0"
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
            from .errors import STLProtocolError
            self._send_json(400, error_envelope(STLProtocolError()))
            return "__handled__"  # type: ignore[return-value]

    def _handle(self, method: str, body: Optional[dict] = None) -> None:
        if body == "__handled__":  # type: ignore[comparison-overlap]
            return
        from urllib.parse import parse_qsl, urlparse
        parsed = urlparse(self.path)
        # keep_blank_values: an explicit empty value (?limit=) must reach the
        # handler for validation (STL-203), not vanish into the default.
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query = {k: v for k, v in pairs}
        try:
            status, resp_body = self.router.dispatch(method, parsed.path, query, body=body)
        except STLError as exc:
            status, resp_body = exc.http_status, error_envelope(exc)
        except Exception as exc:  # pragma: no cover - defensive net
            logger.error("unhandled error on %s %s: %r", method, parsed.path, exc)
            status, resp_body = 500, error_envelope(make_error(
                "STL-999", "An unexpected internal error occurred.", status=500,
                context={"method": method, "path": parsed.path}))
        self._send_json(status, resp_body)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST", body=self._read_body())

    def do_DELETE(self) -> None:  # noqa: N802
        # No DELETE routes; the router answers STL-404 (or 405) itself.
        self._handle("DELETE")


def serve_http(router, controller, host: str, port: int) -> ThreadingHTTPServer:
    _HTTPHandler.router = router
    _HTTPHandler.controller = controller
    httpd = ThreadingHTTPServer((host, port), _HTTPHandler)
    threading.Thread(target=httpd.serve_forever, name="stls-http", daemon=True).start()
    return httpd


def _ingest_loop(cfg: SettlementConfig, engine: SettlementEngine,
                 client: PositionKeeperClient, stop: threading.Event) -> None:
    """Background pull of S6 positions into today's run until ``stop`` is set."""
    interval_s = cfg.ingest.interval_ms / 1000.0
    while not stop.is_set():
        stop.wait(interval_s)
        if stop.is_set():
            break
        rows = client.pull_positions()
        if rows is None:
            if client.consecutive_failures == 1 or \
                    client.consecutive_failures % cfg.ingest.fail_log_limit == 0:
                logger.warning("S6 position pull failed (consecutive=%d); will retry next cycle",
                               client.consecutive_failures)
            continue
        try:
            engine.ingest_from_positions(rows)
        except STLError as exc:
            logger.warning("ingest settle failed: %s %s", exc.code, exc.message)
    logger.info("ingest loop stopped")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="stls.main", description="settlement-service (S14)")
    parser.add_argument("--bind", default=None, help="bind host (default from config)")
    parser.add_argument("--port", type=int, default=None, help="listen port (default 7740)")
    parser.add_argument("--positions-url", dest="positions_url", default=None,
                        help="position-keeper (S6) base URL for background ingest "
                             "(default http://127.0.0.1:7660; set to the container DNS "
                             "name when running in the platform network)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    try:
        cfg = load_config(args)
    except STLError as exc:
        print(json.dumps(error_envelope(exc)), file=sys.stderr)
        return 1

    engine = SettlementEngine(cfg, clock=SystemClock())
    client = PositionKeeperClient(
        cfg.ingest.position_keeper_url,
        connect_timeout_ms=cfg.server.upstream_pull_timeout_ms,
        read_timeout_s=cfg.server.upstream_read_timeout_s)
    controller = SettlementController(cfg=cfg, engine=engine, ingest_client=client)
    router = build_router(controller)

    try:
        httpd = serve_http(router, controller, cfg.server.host, cfg.server.port)
    except OSError as exc:
        print(json.dumps(error_envelope(make_error(
            "STL-101", f"cannot bind {cfg.server.host}:{cfg.server.port}", status=500,
            context={"detail": str(exc)}))), file=sys.stderr)
        return 1

    logger.info("settlement-service v%s starting on %s:%d (env=%s, ingest=%s)",
                cfg.version, cfg.server.host, cfg.server.port, cfg.env,
                "on" if cfg.ingest.enabled else "off")

    stop = threading.Event()

    def _sig(signum, frame):  # noqa: ARG001 - signal handler signature
        logger.info("signal %s; shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    ingest_thread = None
    if cfg.ingest.enabled:
        ingest_thread = threading.Thread(
            target=_ingest_loop, args=(cfg, engine, client, stop),
            name="stls-ingest", daemon=True)
        ingest_thread.start()

    try:
        while not stop.is_set():
            stop.wait(0.25)
    finally:
        httpd.shutdown()
        if ingest_thread is not None:
            ingest_thread.join(timeout=2.0)
    logger.info("shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
