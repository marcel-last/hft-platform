"""data_quality_monitor — service entrypoint and main loop.

Boot sequence:

1.  Validate configuration (abort with a structured error on failure).
2.  Build the :class:`QualityEngine` and wire the upstream clients.
3.  Start the HTTP API thread.
4.  Enter the aggregation loop:

        every ``ingest.poll_interval_ms``:
            pull S1 /quality  -> per-symbol feed metrics
            pull S2 /books    -> per-(symbol, venue) book health
            engine.ingest_and_score(...)  -> recompute scores + hysteresis
            fan out any new degradations to S10 (best-effort)

Run with::

    python -m dqm.main --gateway http://localhost:7610 \\
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
from .controller import QualityController
from .errors import DQMConfigError
from .models import BookHealthRow, now_ns
from .quality_engine import QualityEngine
from .clients import AlertingClient, BookBuilderClient, GatewayClient
from .router import build_router

logger = logging.getLogger("dqm.main")


def load_config(env_overrides: Optional[Dict[str, str]] = None) -> ServiceConfig:
    cfg = ServiceConfig()
    if env_overrides and "DQM_ENV" in env_overrides:
        cfg = ServiceConfig(env=env_overrides["DQM_ENV"])
    errors = validate_config(cfg)
    if errors:
        raise DQMConfigError("configuration validation failed", context={"errors": errors})
    return cfg


# ---------------------------------------------------------------------------
# HTTP transport (std-only, mirrors S1/S2/S3/S5/S6's pattern)
# ---------------------------------------------------------------------------

class _HTTPHandler(BaseHTTPRequestHandler):
    server_version = "DQM/1.0"
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
    threading.Thread(target=httpd.serve_forever, name="dqm-http", daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class QualityRuntime:
    """Owns the aggregation loop and shared state."""

    def __init__(self, cfg: ServiceConfig, gateway_url: Optional[str] = None,
                 books_url: Optional[str] = None) -> None:
        self.cfg = cfg
        self.controller = QualityController()
        self.engine = QualityEngine(cfg)
        self.gateway = GatewayClient(base_url=gateway_url or cfg.ingest.market_data_gateway_url)
        self.books = BookBuilderClient(base_url=books_url or cfg.ingest.order_book_builder_url)
        self.alerting = AlertingClient(base_url=cfg.alerting.alert_service_url)
        self.controller.engine = self.engine
        self.polls = 0

    def boot(self) -> None:
        logger.info("starting %s v%s (env=%s)", self.cfg.name, self.cfg.version, self.cfg.env)

    def _pull_feed_report(self) -> Optional[Dict]:
        try:
            return self.gateway.quality_report()
        except Exception as exc:  # noqa: BLE001 - best-effort refresh
            logger.debug("S1 /quality poll failed: %s", exc)
            self.engine.upstream_failures += 1
            return None

    def _pull_book_rows(self) -> List[BookHealthRow]:
        try:
            raw_books = self.books.books()
        except Exception as exc:  # noqa: BLE001 - best-effort refresh
            logger.debug("S2 /books poll failed: %s", exc)
            self.engine.upstream_failures += 1
            return []
        rows: List[BookHealthRow] = []
        for b in raw_books:
            if isinstance(b, dict):
                try:
                    rows.append(BookHealthRow.from_dict(b))
                except Exception as exc:  # noqa: BLE001 - skip malformed row
                    logger.debug("skipping malformed S2 book row: %s", exc)
        return rows

    def _fan_out(self, events) -> int:
        sent = 0
        for event in events:
            if self.alerting.send_degradation(event.to_dict()):
                sent += 1
        return sent

    def aggregate_once(self) -> Dict[str, int]:
        """One aggregation iteration. Returns per-stage counts."""
        feed_report = self._pull_feed_report()
        book_rows = self._pull_book_rows()
        new_degradations = self.engine.ingest_and_score(feed_report, book_rows)
        alerts_sent = self._fan_out(new_degradations)
        return {
            "feed_symbols": len((feed_report or {}).get("symbols", {})),
            "book_rows": len(book_rows),
            "new_degradations": len(new_degradations),
            "alerts_sent": alerts_sent,
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
                counts = self.aggregate_once()
                self.polls += 1
                if self.polls % 200 == 0:
                    logger.info("aggregation stats: polls=%d last=%s engine=%s",
                                self.polls, counts, self.engine.stats_view())
            except Exception:  # noqa: BLE001 - the loop must never die
                logger.exception("aggregation iteration failed")
            i += 1
            if max_iterations is not None and i >= max_iterations:
                break
            deadline = time.monotonic() + interval_s
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        logger.info("shutdown after %d polls", self.polls)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="dqm.main", description="data-quality-monitor")
    parser.add_argument("--gateway", default=None, help="market-data-gateway base URL")
    parser.add_argument("--books", default=None, help="order-book-builder base URL")
    parser.add_argument("--iterations", type=int, default=None,
                        help="stop after N aggregation iterations (testing)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config(None)
    runtime = QualityRuntime(cfg, gateway_url=args.gateway, books_url=args.books)
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
