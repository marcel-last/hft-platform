"""risk_manager — service entrypoint and main loop.

Boot sequence:

1.  Validate configuration (abort with a structured error on failure).
2.  Build the :class:`RiskEngine` and wire the upstream/downstream clients.
3.  Start the HTTP API thread.
4.  Enter the real-time exposure loop:

        every ``ingest.poll_interval_ms``:
            for each tracked symbol: fetch S2 top-of-book -> set reference price
            fetch S4 open orders  -> refresh the open-order snapshot
            fetch S4 fills (new)  -> apply to authoritative positions

Run with::

    python -m rkm.main --books http://localhost:7620 \
        --execution http://localhost:7640
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
from .controller import RiskController
from .errors import RKMConfigError
from .models import RiskSide, now_ns
from .risk_engine import RiskEngine
from .clients import AlertingClient, AuditClient, BookBuilderClient, ExecutionClient
from .router import build_router

logger = logging.getLogger("rkm.main")


def load_config(env_overrides: Optional[Dict[str, str]] = None) -> ServiceConfig:
    cfg = ServiceConfig()
    if env_overrides and "RKM_ENV" in env_overrides:
        cfg = ServiceConfig(env=env_overrides["RKM_ENV"])
    errors = validate_config(cfg)
    if errors:
        raise RKMConfigError("configuration validation failed", context={"errors": errors})
    return cfg


# ---------------------------------------------------------------------------
# HTTP transport (std-only, mirrors S1/S2/S3's pattern)
# ---------------------------------------------------------------------------

class _HTTPHandler(BaseHTTPRequestHandler):
    server_version = "RKM/1.0"
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
    threading.Thread(target=httpd.serve_forever, name="rkm-http", daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class RiskRuntime:
    """Owns the real-time exposure loop and shared state."""

    def __init__(self, cfg: ServiceConfig, books_url: Optional[str] = None,
                 execution_url: Optional[str] = None) -> None:
        self.cfg = cfg
        self.controller = RiskController()
        self.engine = RiskEngine(cfg)
        self.books = BookBuilderClient(base_url=books_url or cfg.ingest.book_builder_url)
        self.execution = ExecutionClient(base_url=execution_url or cfg.ingest.execution_gateway_url)
        self.alerting = AlertingClient()
        self.audit = AuditClient()
        self.controller.engine = self.engine
        self.controller.execution = self.execution
        self.controller.alerting = self.alerting
        self.controller.audit = self.audit
        self._seen_fill_ids: set = set()
        self.polls = 0

    def boot(self) -> None:
        logger.info("starting %s v%s (env=%s)", self.cfg.name, self.cfg.version, self.cfg.env)

    def _refresh_reference_prices(self) -> int:
        """Pull S2 top-of-book for every tracked symbol; set reference prices."""
        updated = 0
        symbols = list(self.engine._positions.keys())
        if not symbols:
            # No positions yet: nothing to price.  (Exposure math falls back to
            # average entry cost, so this is safe.)
            return 0
        for symbol in symbols:
            try:
                views = self.books.top_of_book(symbol)
            except Exception as exc:  # noqa: BLE001 - best-effort refresh
                logger.debug("ToB poll failed for %s: %s", symbol, exc)
                continue
            for view in views:
                mid = float(view.get("mid", 0.0) or 0.0)
                if mid > 0:
                    self.engine.set_reference_price(symbol, mid)
                    updated += 1
                    break
        return updated

    def _refresh_open_orders(self) -> int:
        try:
            orders = self.execution.open_orders()
        except Exception as exc:  # noqa: BLE001 - best-effort refresh
            logger.debug("open-order poll failed: %s", exc)
            return 0
        self.engine.set_open_orders(orders)
        return len(orders)

    def _apply_new_fills(self) -> int:
        try:
            fills = self.execution.fills(limit=512)
        except Exception as exc:  # noqa: BLE001 - best-effort refresh
            logger.debug("fill poll failed: %s", exc)
            return 0
        applied = 0
        for fill in fills:
            fid = str(fill.get("id", ""))
            if fid and fid in self._seen_fill_ids:
                continue
            if fid:
                self._seen_fill_ids.add(fid)
                if len(self._seen_fill_ids) > 100_000:
                    # Bound memory: drop the oldest half (a set has no order, so
                    # simply clear; fills are re-applied idempotently by symbol state).
                    self._seen_fill_ids = set(list(self._seen_fill_ids)[-50_000:])
            try:
                side = RiskSide(str(fill.get("side", "BUY")).upper())
            except ValueError:
                continue
            symbol = str(fill.get("symbol", ""))
            if not symbol:
                continue
            qty = int(fill.get("qty", 0))
            price = float(fill.get("price", fill.get("px", 0.0)))
            ts_ns = int(fill.get("ts_ns", now_ns()))
            self.engine.apply_fill(symbol, side, qty, price, ts_ns)
            applied += 1
        return applied

    def ingest_once(self) -> Dict[str, int]:
        """One real-time exposure refresh. Returns per-stage counts."""
        return {
            "ref_prices": self._refresh_reference_prices(),
            "open_orders": self._refresh_open_orders(),
            "fills_applied": self._apply_new_fills(),
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
                counts = self.ingest_once()
                self.polls += 1
                if self.polls % 500 == 0:
                    logger.info("exposure stats: polls=%d last=%s engine=%s",
                                self.polls, counts, self.engine.stats_view())
            except Exception:  # noqa: BLE001 - the loop must never die
                logger.exception("exposure refresh iteration failed")
            i += 1
            if max_iterations is not None and i >= max_iterations:
                break
            deadline = time.monotonic() + interval_s
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        logger.info("shutdown after %d polls", self.polls)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="rkm.main", description="risk-manager")
    parser.add_argument("--books", default=None, help="order-book-builder base URL")
    parser.add_argument("--execution", default=None, help="execution-gateway base URL")
    parser.add_argument("--iterations", type=int, default=None,
                        help="stop after N exposure refresh iterations (testing)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config(None)
    runtime = RiskRuntime(cfg, books_url=args.books, execution_url=args.execution)
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
