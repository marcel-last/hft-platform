"""market_data_gateway — service entrypoint and main loop.

Boot sequence:

1.  Load + validate configuration (abort with a structured error on failure).
2.  Construct the shared state bundle (:class:`GatewayController`).
3.  Start the quality monitor sweep thread.
4.  Connect all venue feeds (in mock mode this uses the deterministic
    :class:`~mdg.mock_feed.MockVenueTransport`).
5.  Enter the main poll loop:

        feed_client.poll_all()
            -> normalize each raw message (normalizer)
            -> write normalized quotes into the sharded ring buffer
            -> observe in the quality monitor
            -> fan out to registered subscribers (in-process push channel)
            -> keep a bounded per-symbol recent-quotes cache for the API

6.  Serve the HTTP API on a background thread pool until SIGTERM/SIGINT.

Run with::

    python -m mdg.main --mock          # deterministic offline replay
    python -m mdg.main                 # live venue feeds (requires network)
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
from .controller import GatewayController
from .errors import ConfigError
from .feed_client import FeedClient
from .models import (
    ControlEventKind,
    RawHeartbeat,
    RawQuote,
    RawTrade,
    quote_to_wire,
    trade_to_wire,
    now_ns,
)
from .normalizer import QuoteNormalizer
from .quality_monitor import QualityMonitor
from .ring_buffer import ShardedRingBuffer
from .router import build_router

logger = logging.getLogger("mdg.main")


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_config(env_overrides: Optional[Dict[str, str]] = None) -> ServiceConfig:
    """Build the service config, applying env overrides and validating."""
    cfg = ServiceConfig()
    if env_overrides:
        # supported overrides: MDG_ENV, MDG_LISTEN_PORT
        if "MDG_ENV" in env_overrides:
            from dataclasses import replace
            cfg = ServiceConfig(env=env_overrides["MDG_ENV"])
        if "MDG_LISTEN_PORT" in env_overrides:
            from dataclasses import replace
            port = int(env_overrides["MDG_LISTEN_PORT"])
            cfg = ServiceConfig(network=replace(cfg.network, listen_port=port))
    errors = validate_config(cfg)
    if errors:
        raise ConfigError(
            "configuration validation failed",
            context={"errors": errors},
        )
    return cfg


# ---------------------------------------------------------------------------
# HTTP transport (dependency-free, stdlib only)
# ---------------------------------------------------------------------------

class _HTTPHandler(BaseHTTPRequestHandler):
    """Minimal JSON request handler bound to a router + controller."""

    server_version = "MDG/1.0"

    # class-level references set by serve_http()
    router = None
    controller: Optional[GatewayController] = None

    def log_message(self, fmt: str, *args) -> None:  # silence default stderr logging
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)  # drain body; handlers don't use it
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        status, body = self.router.dispatch(method, parsed.path, query)
        self._send_json(status, body)


def serve_http(router, controller: GatewayController, host: str, port: int) -> ThreadingHTTPServer:
    _HTTPHandler.router = router
    _HTTPHandler.controller = controller
    httpd = ThreadingHTTPServer((host, port), _HTTPHandler)
    thread = threading.Thread(target=httpd.serve_forever, name="mdg-http", daemon=True)
    thread.start()
    return httpd


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

class GatewayRuntime:
    """Owns the hot path: feed -> normalize -> buffer -> fan-out."""

    def __init__(self, cfg: ServiceConfig, use_mock: bool) -> None:
        self.cfg = cfg
        self.controller = GatewayController()
        self.normalizer = QuoteNormalizer(cfg.normalization)
        self.quality_monitor = QualityMonitor(window_seconds=10.0)
        self.quote_buffer = ShardedRingBuffer(
            shard_count=cfg.buffering.shard_count,
            capacity_per_shard=cfg.buffering.quote_ring_size // cfg.buffering.shard_count,
        )
        if use_mock:
            from .mock_feed import MockVenueTransport
            factory = lambda: MockVenueTransport(seed=42)
        else:
            factory = None
        self.feed_client = FeedClient(transport_factory=factory)

        # wire controller
        self.controller.feed_client = self.feed_client
        self.controller.normalizer = self.normalizer
        self.controller.quality_monitor = self.quality_monitor
        self.controller.quote_buffer = self.quote_buffer
        self.controller._recent: Dict[str, List[dict]] = {}
        self._recent_max = 512

        # counters for the periodic stats log
        self.polls = 0
        self.quotes_processed = 0
        self.last_stats_ns = now_ns()

    def boot(self) -> None:
        logger.info("starting %s v%s (env=%s, mock=%s)",
                    self.cfg.name, self.cfg.version, self.cfg.env, "yes" if self.feed_client else "no")
        events = self.feed_client.connect_all()
        for ev in events:
            logger.info("boot event: %s on %s — %s", ev.kind.value, ev.venue_id, ev.detail)
        self.quality_monitor.start()

    def _fan_out(self, wire_quote: dict) -> None:
        """Push one normalized quote to every registered subscriber."""
        for sid, info in self.controller.subscribers.items():
            symbols = info.get("symbols") or []
            if symbols and wire_quote["sym"] not in symbols:
                continue
            push = info.get("push")  # callable set by the subscriber layer
            if callable(push):
                try:
                    push(wire_quote)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("subscriber %s push failed; dropping", sid)

    def _remember_recent(self, wire_quote: dict) -> None:
        """Keep a bounded per-symbol cache for the /history API."""
        recent = self.controller._recent
        symbol = wire_quote["sym"]
        buf = recent.setdefault(symbol, [])
        buf.append(wire_quote)
        if len(buf) > self._recent_max:
            del buf[: len(buf) - self._recent_max]

    def poll_once(self) -> int:
        """One iteration of the hot loop.  Returns messages processed."""
        raw_messages, events = self.feed_client.poll_all()
        for ev in events:
            if ev.kind == ControlEventKind.STALENESS_BREACH:
                logger.warning("quality: %s", ev.detail)
            elif ev.kind == ControlEventKind.HEARTBEAT_MISSED:
                logger.warning("heartbeat missed on %s: %s", ev.venue_id, ev.detail)
            elif ev.kind == ControlEventKind.SEQUENCE_GAP:
                logger.warning("sequence gap on %s (size=%s)", ev.venue_id, ev.sequence_gap_size)

        processed = 0
        for msg in raw_messages:
            if isinstance(msg, RawHeartbeat):
                continue
            emit_ts = now_ns()
            quote = None
            wire = None
            if isinstance(msg, RawQuote):
                quote = self.normalizer.normalize_quote(msg, emit_ts)
                if quote is not None:
                    from .models import quote_to_wire
                    wire = quote_to_wire(quote)
                    self.quote_buffer.write(quote.canonical_symbol, wire)
                    self.quality_monitor.observe(quote)
                    self._fan_out(wire)
                    self._remember_recent(wire)
                    processed += 1
            elif isinstance(msg, dict) and "sym" in msg:
                # pre-normalized wire quote (in-process push from another
                # gateway instance or a test harness); buffer + remember only
                self.quote_buffer.write(msg["sym"], msg)
                self._remember_recent(msg)
                processed += 1
            elif isinstance(msg, RawTrade):
                trade = self.normalizer.normalize_trade(msg, emit_ts)
                if trade is not None:
                    from .models import trade_to_wire
                    wire_t = trade_to_wire(trade)
                    self.quote_buffer.write(trade.canonical_symbol, {"trade": wire_t})
                    processed += 1
        return processed

    def periodic_stats(self) -> None:
        """Log aggregate stats at most once per second."""
        now = now_ns()
        if (now - self.last_stats_ns) < 1_000_000_000:
            return
        span_s = (now - self.last_stats_ns) / 1_000_000_000.0
        rate = self.quotes_processed / span_s if span_s > 0 else 0.0
        buf_stats = self.quote_buffer.stats()
        avg_fill = sum(s["fill_pct"] for s in buf_stats) / max(1, len(buf_stats))
        dropped = sum(s["dropped"] for s in buf_stats)
        logger.info(
            "stats: %.0f quotes/s | buffer fill=%.2f%% dropped=%d | normalizer=%s",
            rate, avg_fill * 100.0, dropped, self.normalizer.stats,
        )
        self.last_stats_ns = now

    def run(self) -> None:
        """Main loop until stopped."""
        stop = threading.Event()

        def _sig_handler(signum, frame):
            logger.info("received signal %s; shutting down", signum)
            stop.set()

        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGINT, _sig_handler)

        poll_interval_s = self.cfg.network.epoll_wait_timeout_ms / 1000.0
        while not stop.is_set():
            try:
                n = self.poll_once()
                self.quotes_processed += n
                self.polls += 1
                self.periodic_stats()
            except Exception:
                logger.exception("main loop iteration failed; continuing")
            # sleep in small slices so signal handling stays responsive
            deadline = time.monotonic() + poll_interval_s
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))

        self.quality_monitor.stop()
        for conn in self.feed_client.connections.values():
            conn.close()
        logger.info("shutdown complete after %d polls", self.polls)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mdg.main", description="market-data-gateway")
    parser.add_argument("--mock", action="store_true", help="use deterministic mock venue feeds")
    parser.add_argument("--env", default=None, choices=[None, "production", "staging", "test"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    env_overrides = {}
    if args.env:
        env_overrides["MDG_ENV"] = args.env
    cfg = load_config(env_overrides or None)

    runtime = GatewayRuntime(cfg, use_mock=args.mock)
    router = build_router(runtime.controller)
    httpd = serve_http(router, runtime.controller, cfg.network.listen_host, cfg.network.listen_port)
    logger.info("HTTP API listening on %s:%d", cfg.network.listen_host, cfg.network.listen_port)

    runtime.boot()
    try:
        runtime.run()
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
