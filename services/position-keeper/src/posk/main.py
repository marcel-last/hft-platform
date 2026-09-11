"""position_keeper — service entrypoint and main loop.

Boot sequence:

1.  Validate configuration (abort with a structured error on failure).
2.  Build the :class:`PositionEngine` and wire the upstream clients.
3.  Start the HTTP API thread.
4.  Enter the ingest loop:

        every ``ingest.poll_interval_ms``:
            poll S4 /fills -> apply each new fill exactly once to the ledger
            (every ``snapshot.snapshot_interval_ms``) refresh S2 reference
            prices for marked-to-market snapshots and capture a snapshot

Run with::

    python -m posk.main --execution http://localhost:7640 \
        --books http://localhost:7620
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
from .controller import PositionController
from .errors import POSKConfigError
from .models import now_ns
from .position_engine import PositionEngine
from .clients import BookBuilderClient, ExecutionClient
from .router import build_router

logger = logging.getLogger("posk.main")


def load_config(env_overrides: Optional[Dict[str, str]] = None) -> ServiceConfig:
    cfg = ServiceConfig()
    if env_overrides and "POSK_ENV" in env_overrides:
        cfg = ServiceConfig(env=env_overrides["POSK_ENV"])
    errors = validate_config(cfg)
    if errors:
        raise POSKConfigError("configuration validation failed", context={"errors": errors})
    return cfg


# ---------------------------------------------------------------------------
# HTTP transport (std-only, mirrors S1/S2/S3/S5's pattern)
# ---------------------------------------------------------------------------

class _HTTPHandler(BaseHTTPRequestHandler):
    server_version = "POSK/1.0"
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
    threading.Thread(target=httpd.serve_forever, name="posk-http", daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class PositionRuntime:
    """Owns the fill-ingest loop and shared state."""

    def __init__(self, cfg: ServiceConfig, execution_url: Optional[str] = None,
                 books_url: Optional[str] = None) -> None:
        self.cfg = cfg
        self.controller = PositionController()
        self.engine = PositionEngine(cfg)
        self.execution = ExecutionClient(base_url=execution_url or cfg.ingest.execution_gateway_url)
        self.books = BookBuilderClient(base_url=books_url or cfg.snapshot.book_builder_url)
        self.controller.engine = self.engine
        self.controller.execution = self.execution
        self.controller.books = self.books
        self.polls = 0
        self._last_snapshot_ns = 0

    def boot(self) -> None:
        logger.info("starting %s v%s (env=%s)", self.cfg.name, self.cfg.version, self.cfg.env)

    def _apply_new_fills(self) -> int:
        try:
            fills = self.execution.fills(limit=self.cfg.ingest.max_fills_per_poll)
        except Exception as exc:  # noqa: BLE001 - best-effort refresh
            logger.debug("fill poll failed: %s", exc)
            return 0
        applied = 0
        for fill in fills:
            try:
                self.engine.apply_fill_dict(fill)
                applied += 1
            except Exception as exc:  # noqa: BLE001 - skip malformed/duplicate
                logger.debug("skipped fill %s: %s", fill.get("id"), exc)
        return applied

    def _refresh_reference_prices(self) -> int:
        """Pull S2 top-of-book for each open position's symbol; set ref prices."""
        updated = 0
        symbols = sorted({p.symbol for p in self.engine.list_positions(include_flat=False)})
        if not symbols:
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

    def _maybe_snapshot(self) -> bool:
        interval_ns = self.cfg.snapshot.snapshot_interval_ms * 1_000_000
        now = now_ns()
        if now - self._last_snapshot_ns >= interval_ns:
            self.engine.take_snapshot()
            self._last_snapshot_ns = now
            return True
        return False

    def ingest_once(self) -> Dict[str, int]:
        """One ingest iteration. Returns per-stage counts."""
        fills_applied = self._apply_new_fills()
        refs_updated = self._refresh_reference_prices()
        snapshotted = 1 if self._maybe_snapshot() else 0
        return {
            "fills_applied": fills_applied,
            "ref_prices_updated": refs_updated,
            "snapshots_taken": snapshotted,
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
                    logger.info("ingest stats: polls=%d last=%s engine=%s",
                                self.polls, counts, self.engine.stats_view())
            except Exception:  # noqa: BLE001 - the loop must never die
                logger.exception("ingest iteration failed")
            i += 1
            if max_iterations is not None and i >= max_iterations:
                break
            deadline = time.monotonic() + interval_s
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        logger.info("shutdown after %d polls", self.polls)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="posk.main", description="position-keeper")
    parser.add_argument("--execution", default=None, help="execution-gateway base URL")
    parser.add_argument("--books", default=None, help="order-book-builder base URL")
    parser.add_argument("--iterations", type=int, default=None,
                        help="stop after N ingest iterations (testing)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config(None)
    runtime = PositionRuntime(cfg, execution_url=args.execution, books_url=args.books)
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
