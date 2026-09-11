"""config_service — service entrypoint.

Boot sequence:

1.  Validate configuration (abort with a structured error on failure).
2.  Build the :class:`ConfigStore` and wire the controller + best-effort
    downstream clients (S10 alerting, S13 audit).
3.  Start the HTTP API thread.
4.  Block until SIGINT / SIGTERM.

Unlike the data-plane services, S11 has **no background ingest loop**: it is a
pure request/response + long-poll provider.  Long-poll requests are served by
the threaded HTTP server, so each ``/changes/watch`` occupies one worker thread
for up to its timeout (bounded by ``poll.max_timeout_ms``).

Run with::

    python -m cfgs.main
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .config import CONFIG, ServiceConfig, validate_config
from .config_store import ConfigStore
from .controller import ConfigController
from .errors import CFGConfigError
from .router import build_router

logger = logging.getLogger("cfgs.main")


def load_config(env_overrides: Optional[Dict[str, str]] = None) -> ServiceConfig:
    cfg = ServiceConfig()
    if env_overrides and "CFGS_ENV" in env_overrides:
        cfg = ServiceConfig(env=env_overrides["CFGS_ENV"])
    errors = validate_config(cfg)
    if errors:
        raise CFGConfigError("configuration validation failed", context={"errors": errors})
    return cfg


# ---------------------------------------------------------------------------
# HTTP transport (std-only, mirrors S1/S2/S3/S5/S6/S8/S9's pattern)
# ---------------------------------------------------------------------------

class _HTTPHandler(BaseHTTPRequestHandler):
    server_version = "CFGS/1.0"
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
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _handle(self, method: str, body: Optional[dict] = None) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        status, resp_body = self.router.dispatch(method, parsed.path, query, body=body)
        self._send_json(status, resp_body)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST", body=self._read_body())

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT", body=self._read_body())

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE", body=self._read_body())


def serve_http(router, controller, host: str, port: int) -> ThreadingHTTPServer:
    _HTTPHandler.router = router
    _HTTPHandler.controller = controller
    httpd = ThreadingHTTPServer((host, port), _HTTPHandler)
    threading.Thread(target=httpd.serve_forever, name="cfgs-http", daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class ConfigRuntime:
    """Owns the store and shared state. S11 has no background loop."""

    def __init__(self, cfg: ServiceConfig) -> None:
        self.cfg = cfg
        self.store = ConfigStore(cfg)
        self.controller = ConfigController()
        self.controller.store = self.store

    def boot(self) -> None:
        logger.info("starting %s v%s (env=%s)", self.cfg.name, self.cfg.version, self.cfg.env)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="cfgs.main", description="config-service")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config(None)
    runtime = ConfigRuntime(cfg)
    router = build_router(runtime.controller)
    httpd = serve_http(router, runtime.controller, "0.0.0.0", cfg.listen_port)
    logger.info("HTTP API listening on :%d", cfg.listen_port)

    runtime.boot()

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
