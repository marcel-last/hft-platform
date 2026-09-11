"""strategy_engine — service entrypoint and main loop.

Boot sequence:

1.  Validate configuration (abort with a structured error on failure).
2.  Build the :class:`SignalEngine`, seed its tick-size map from the gateway's
    symbol table, and register the three built-in strategies.
3.  Start the HTTP API thread.
4.  Enter the ingest loop:

        for each subscribed symbol:
            views = books.top_of_book(symbol)          # S2 (preferred)
            if no views: quotes = gateway.poll_quotes(symbol)   # S1 fallback
            feed each tick to the engine -> signals + intents
            push new intents to the execution gateway (S4)

Run with::

    python -m ste.main --gateway http://localhost:7610 \
        --books http://localhost:7620 --execution http://localhost:7640
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
from .controller import StrategyController
from .errors import ExecutionGatewayError, STEConfigError, UpstreamUnreachableError
from .signal_engine import SignalEngine
from .clients import BookBuilderClient, ExecutionClient, GatewayClient
from .models import now_ns
from .router import build_router

logger = logging.getLogger("ste.main")


def load_config(env_overrides: Optional[Dict[str, str]] = None) -> ServiceConfig:
    cfg = ServiceConfig()
    if env_overrides and "STE_ENV" in env_overrides:
        cfg = ServiceConfig(env=env_overrides["STE_ENV"])
    errors = validate_config(cfg)
    if errors:
        raise STEConfigError("configuration validation failed", context={"errors": errors})
    return cfg


# ---------------------------------------------------------------------------
# HTTP transport (std-only, mirrors S1/S2's pattern)
# ---------------------------------------------------------------------------

class _HTTPHandler(BaseHTTPRequestHandler):
    server_version = "STE/1.0"
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
        body: Optional[dict] = None
        if length:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                body = {}
        self._handle("POST", body=body)

    def _handle(self, method: str, body: Optional[dict] = None) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        status, resp_body = self.router.dispatch(method, parsed.path, query, body=body)
        self._send_json(status, resp_body)


def serve_http(router, controller, host: str, port: int) -> ThreadingHTTPServer:
    _HTTPHandler.router = router
    _HTTPHandler.controller = controller
    httpd = ThreadingHTTPServer((host, port), _HTTPHandler)
    threading.Thread(target=httpd.serve_forever, name="ste-http", daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class StrategyRuntime:
    """Owns the ingest loop and shared state."""

    def __init__(self, cfg: ServiceConfig, gateway_url: Optional[str] = None,
                 books_url: Optional[str] = None, execution_url: Optional[str] = None) -> None:
        self.cfg = cfg
        self.controller = StrategyController()
        self.engine = SignalEngine(cfg)
        self.gateway = GatewayClient(base_url=gateway_url or cfg.ingest.gateway_url)
        self.books = BookBuilderClient(base_url=books_url or cfg.ingest.book_builder_url)
        self.execution = ExecutionClient(base_url=execution_url or cfg.emit.execution_gateway_url)
        self.controller.engine = self.engine
        self.controller.gateway = self.gateway
        self.controller.books = self.books
        self.controller.execution = self.execution
        self._last_quote_rt: Dict[str, int] = {}
        self.polls = 0

    def _seed_tick_sizes(self) -> None:
        """Load tick sizes from the gateway symbol table (best effort)."""
        try:
            for row in self.gateway.symbols():
                sym = row.get("canonical_symbol")
                tick = row.get("tick_size")
                if sym and tick:
                    self.engine.set_tick_size(sym, float(tick))
            logger.info("seeded %d tick sizes from gateway", len(self.gateway.symbols()))
        except UpstreamUnreachableError as exc:
            logger.warning("could not load symbol table at boot (%s); using defaults", exc.message)

    def boot(self) -> None:
        logger.info("starting %s v%s (env=%s)", self.cfg.name, self.cfg.version, self.cfg.env)
        self.engine.register_default_strategies()
        self._seed_tick_sizes()

    def _push_intent(self, intent) -> None:
        if not self.cfg.emit.push_on_emit:
            return
        try:
            resp = self.execution.push_intent(intent.to_dict())
            # S4 echoes the accepted order id; mark our intent acknowledged.
            self.engine.mark_intent_acknowledged(intent.id)
            logger.debug("intent %s accepted by execution gateway (%s)",
                         intent.id, resp.get("id", "ok"))
        except ExecutionGatewayError as exc:
            logger.warning("execution gateway rejected intent %s: %s", intent.id, exc.message)

    def ingest_once(self) -> int:
        """One ingest iteration across all subscribed symbols. Returns tick count."""
        ticks = 0
        for symbol in self.cfg.ingest.subscribe_symbols:
            views = None
            try:
                views = self.books.top_of_book(symbol)
            except UpstreamUnreachableError as exc:
                logger.debug("book ToB poll failed for %s: %s", symbol, exc.message)
            if views:
                for book in views:
                    _, intents = self.engine.apply_book_view(book)
                    ticks += 1
                    for intent in intents:
                        self._push_intent(intent)
                continue
            # Fallback to raw quotes when no book view is available yet.
            try:
                quotes = self.gateway.poll_quotes(symbol, limit=self.cfg.ingest.quote_poll_limit)
            except UpstreamUnreachableError as exc:
                logger.debug("quote poll failed for %s: %s", symbol, exc.message)
                continue
            last_seen = self._last_quote_rt.get(symbol, 0)
            fresh = [q for q in quotes if int(q.get("rt", 0)) > last_seen]
            if not fresh:
                continue
            for quote in fresh:
                _, intents = self.engine.apply_quote(quote)
                ticks += 1
                for intent in intents:
                    self._push_intent(intent)
            self._last_quote_rt[symbol] = max(int(q.get("rt", 0)) for q in quotes) if quotes else last_seen
        return ticks

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
                    logger.info("ingest stats: polls=%d ticks_last=%d engine=%s",
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
    parser = argparse.ArgumentParser(prog="ste.main", description="strategy-engine")
    parser.add_argument("--gateway", default=None, help="market-data-gateway base URL")
    parser.add_argument("--books", default=None, help="order-book-builder base URL")
    parser.add_argument("--execution", default=None, help="execution-gateway base URL")
    parser.add_argument("--iterations", type=int, default=None,
                        help="stop after N ingest iterations (testing)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config(None)
    runtime = StrategyRuntime(cfg, gateway_url=args.gateway, books_url=args.books,
                              execution_url=args.execution)
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
