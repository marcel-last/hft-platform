"""portfolio_analytics — service entrypoint and main loop.

Boot sequence:

1.  Validate configuration (abort with a structured error on failure).
2.  Build the :class:`AnalyticsEngine` and wire the upstream S6 client.
3.  Start the HTTP API thread.
4.  Enter the refresh loop:

        every ``ingest.poll_interval_ms``:
            pull S6 /positions  -> authoritative position book
            engine.ingest_positions(...)  -> recompute P&L, metrics, VaR

Run with::

    python -m pfa.main --positions http://localhost:7660
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .config import CONFIG, ServiceConfig, validate_config
from .controller import AnalyticsController
from .errors import PFAConfigError
from .models import now_ns
from .analytics_engine import AnalyticsEngine
from .clients import PositionKeeperClient
from .router import build_router

logger = logging.getLogger("pfa.main")


def load_config(env_overrides: Optional[Dict[str, str]] = None) -> ServiceConfig:
    cfg = ServiceConfig()
    if env_overrides and "PFA_ENV" in env_overrides:
        cfg = ServiceConfig(env=env_overrides["PFA_ENV"])
    errors = validate_config(cfg)
    if errors:
        raise PFAConfigError("configuration validation failed", context={"errors": errors})
    return cfg


# ---------------------------------------------------------------------------
# HTTP transport (std-only, mirrors S1/S2/S3/S5/S6/S8's pattern)
# ---------------------------------------------------------------------------

class _HTTPHandler(BaseHTTPRequestHandler):
    server_version = "PFA/1.0"
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
        self._handle("DELETE")


def serve_http(router, controller, host: str, port: int) -> ThreadingHTTPServer:
    _HTTPHandler.router = router
    _HTTPHandler.controller = controller
    httpd = ThreadingHTTPServer((host, port), _HTTPHandler)
    threading.Thread(target=httpd.serve_forever, name="pfa-http", daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class AnalyticsRuntime:
    """Owns the refresh loop and shared state."""

    def __init__(self, cfg: ServiceConfig, positions_url: Optional[str] = None) -> None:
        self.cfg = cfg
        self.controller = AnalyticsController()
        self.engine = AnalyticsEngine(cfg)
        self.positions = PositionKeeperClient(
            base_url=positions_url or cfg.ingest.position_keeper_url
        )
        self.controller.engine = self.engine
        self.polls = 0

    def boot(self) -> None:
        logger.info("starting %s v%s (env=%s)", self.cfg.name, self.cfg.version, self.cfg.env)

    def _pull_positions(self) -> Optional[List[dict]]:
        try:
            return self.positions.positions()
        except Exception as exc:  # noqa: BLE001 - best-effort refresh
            logger.debug("S6 /positions poll failed: %s", exc)
            self.engine.upstream_failures += 1
            return None

    def refresh_once(self) -> Dict[str, int]:
        """One refresh iteration. Returns per-stage counts."""
        rows = self._pull_positions()
        ingested = self.engine.ingest_positions(rows) if rows is not None else 0
        return {
            "positions_ingested": ingested,
            "upstream_ok": rows is not None,
        }

    def run(self, max_iterations: Optional[int] = None) -> None:
        stop = threading.Event()

        def _sig(signum, frame):
            logger.info("signal %s; shutting down", signum)
            stop.set()

        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)

        interval_s = self.cfg.ingest.poll_interval_ms / 1000.0
        i = 0
        while not stop.is_set():
            try:
                counts = self.refresh_once()
                self.polls += 1
                if self.polls % 200 == 0:
                    logger.info("refresh stats: polls=%d last=%s engine=%s",
                                self.polls, counts, self.engine.stats_view())
            except Exception:  # noqa: BLE001 - the loop must never die
                logger.exception("refresh iteration failed")
            i += 1
            if max_iterations is not None and i >= max_iterations:
                break
            deadline = time.monotonic() + interval_s
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        logger.info("shutdown after %d polls", self.polls)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pfa.main", description="portfolio-analytics")
    parser.add_argument("--positions", default=None, help="position-keeper base URL")
    parser.add_argument("--iterations", type=int, default=None,
                        help="stop after N refresh iterations (testing)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config(None)
    runtime = AnalyticsRuntime(cfg, positions_url=args.positions)
    router = build_router(runtime.controller)
    httpd = serve_http(router, runtime.controller, "0.0.0.0", cfg.listen_port)
    logger.info("HTTP API listening on :%d", cfg.listen_port)

    runtime.boot()
    try:
        runtime.run(max_iterations=args.iterations)
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
