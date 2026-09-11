"""order_book_builder — service entrypoint and main loop.

Boot sequence:

1.  Validate configuration (abort with structured error on failure).
2.  Build the :class:`BookEngine` pre-seeded with tick sizes from the shared
    normalization table, and register one book per configured symbol for each
    venue that carries it.
3.  Start the HTTP API thread.
4.  Enter the ingest loop:

        for each subscribed symbol:
            quotes = gateway.poll_quotes(symbol)
            for quote in quotes:
                events = engine.apply_quote(quote)
                controller._record_event(each event)
        engine.sweep_stale() -> record stale events

Run with::

    python -m obb.main --gateway http://localhost:7610
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
from .controller import BookBuilderController
from .errors import OBBConfigError, GatewayUnreachableError
from .book_engine import BookEngine
from .gateway_client import GatewayClient
from .models import now_ns
from .router import build_router

logger = logging.getLogger("obb.main")


def load_config(env_overrides: Optional[Dict[str, str]] = None) -> ServiceConfig:
    cfg = ServiceConfig()
    if env_overrides and "OBB_ENV" in env_overrides:
        from dataclasses import replace
        cfg = ServiceConfig(env=env_overrides["OBB_ENV"])
    errors = validate_config(cfg)
    if errors:
        raise OBBConfigError("configuration validation failed", context={"errors": errors})
    return cfg


# ---------------------------------------------------------------------------
# HTTP transport (std-only, mirrors S1's pattern)
# ---------------------------------------------------------------------------

class _HTTPHandler(BaseHTTPRequestHandler):
    server_version = "OBB/1.0"
    router = None
    controller = None

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        status, body = self.router.dispatch(method, parsed.path, query)
        self._send_json(status, body)


def serve_http(router, controller, host: str, port: int) -> ThreadingHTTPServer:
    _HTTPHandler.router = router
    _HTTPHandler.controller = controller
    httpd = ThreadingHTTPServer((host, port), _HTTPHandler)
    threading.Thread(target=httpd.serve_forever, name="obb-http", daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class BookBuilderRuntime:
    """Owns the ingest loop and shared state."""

    def __init__(self, cfg: ServiceConfig, gateway_url: Optional[str] = None) -> None:
        self.cfg = cfg
        self.controller = BookBuilderController()
        # tick sizes: import from the shared normalization table when available,
        # otherwise fall back to a conservative default
        tick_sizes = self._load_tick_sizes()
        self.engine = BookEngine(tick_sizes=tick_sizes)
        self.gateway = GatewayClient(base_url=gateway_url or cfg.ingest.gateway_url)
        self.controller.engine = self.engine
        self.controller.gateway = self.gateway
        self._last_poll_ns: Dict[str, int] = {}
        self.polls = 0

    @staticmethod
    def _load_tick_sizes() -> Dict[str, float]:
        """Reuse the gateway's normalization table when importable."""
        try:
            from mdg.config import CONFIG as MDG_CONFIG
            return {sym: entry[0] for sym, entry in MDG_CONFIG.normalization.tick_sizes.items()}
        except ImportError:
            logger.warning("mdg package not importable; using default tick sizes")
            return {}

    def _register_books(self) -> None:
        """Create one book per (symbol, venue) pair present in the symbol map."""
        try:
            from mdg.config import CONFIG as MDG_CONFIG
            venue_map = MDG_CONFIG.normalization.canonical_map
        except ImportError:
            venue_map = {}
        for symbol in self.cfg.ingest.subscribe_symbols:
            venues = {venue for (venue, _sym), canon in venue_map.items() if canon == symbol}
            tick = 0.25  # fallback
            try:
                from mdg.config import CONFIG as MDG_CONFIG
                tick = MDG_CONFIG.normalization.tick_sizes.get(symbol, (tick,))[0]
            except ImportError:
                pass
            for venue in sorted(venues) or ["primary"]:
                self.engine.register_book(symbol, venue, tick)
        logger.info("registered %d books", len(self.engine.all_books()))

    def boot(self) -> None:
        logger.info("starting %s v%s (env=%s)", self.cfg.name, self.cfg.version, self.cfg.env)
        self._register_books()
        try:
            self.gateway.subscribe(list(self.cfg.ingest.subscribe_symbols))
        except GatewayUnreachableError as exc:
            logger.warning("gateway not reachable at boot (%s); continuing in offline mode", exc.message)

    def ingest_once(self) -> int:
        """One ingest iteration across all subscribed symbols."""
        applied = 0
        for symbol in self.cfg.ingest.subscribe_symbols:
            try:
                quotes = self.gateway.poll_quotes(symbol, limit=256)
            except GatewayUnreachableError as exc:
                logger.debug("poll failed for %s: %s", symbol, exc.message)
                continue
            # only apply quotes newer than the last poll cursor
            last_seen = self._last_poll_ns.get(symbol, 0)
            fresh = [q for q in quotes if q.get("rt", 0) > last_seen]
            if not fresh:
                continue
            for quote in fresh:
                events = self.engine.apply_quote(quote)
                applied += 1
                for ev in events:
                    self.controller._record_event(ev)
            self._last_poll_ns[symbol] = max(q.get("rt", 0) for q in quotes) if quotes else last_seen
        # staleness sweep at a slower cadence (once per second)
        now = now_ns()
        if not hasattr(self, "_last_sweep_ns") or now - self._last_sweep_ns > 1_000_000_000:
            self._last_sweep_ns = now
            for ev in self.engine.sweep_stale():
                self.controller._record_event(ev)
        return applied

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
                n = self.ingest_once()
                self.polls += 1
                if n and self.polls % 500 == 0:
                    logger.info("ingest stats: polls=%d applied_last=%d engine=%s",
                                self.polls, n, self.engine.stats)
            except Exception:
                logger.exception("ingest iteration failed")
            i += 1
            if max_iterations is not None and i >= max_iterations:
                break
            deadline = time.monotonic() + interval_s
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))
        logger.info("shutdown after %d polls", self.polls)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="obb.main", description="order-book-builder")
    parser.add_argument("--gateway", default=None, help="market-data-gateway base URL")
    parser.add_argument("--iterations", type=int, default=None, help="stop after N ingest iterations (testing)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config(None)
    runtime = BookBuilderRuntime(cfg, gateway_url=args.gateway)
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
