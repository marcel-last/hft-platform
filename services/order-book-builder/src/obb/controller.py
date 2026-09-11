"""order_book_builder — HTTP controller.

Endpoints:

    GET  /healthz            liveness
    GET  /readyz             readiness (books built?)
    GET  /books              list of all maintained books with health + ToB
    GET  /book/{symbol}      full snapshot of one book (all venues)
    GET  /tob/{symbol}       top-of-book only (fast path for strategies)
    GET  /events             recent material book events
    POST /rebuild/{symbol}   force a rebuild from the gateway snapshot
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from .config import CONFIG
from .errors import OBBError, error_envelope
from .models import BookEvent, now_ns

logger = logging.getLogger("obb.controller")


class BookBuilderController:
    """Shared state bundle for the HTTP handlers."""

    def __init__(self) -> None:
        self.engine = None          # BookEngine (wired by main)
        self.gateway = None         # GatewayClient (wired by main)
        self.recent_events: List[BookEvent] = []
        self._max_events = 1024

    def _record_event(self, event: BookEvent) -> None:
        self.recent_events.append(event)
        if len(self.recent_events) > self._max_events:
            del self.recent_events[: len(self.recent_events) - self._max_events]

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def healthz(self) -> Tuple[int, Dict[str, Any]]:
        return 200, {"status": "ok", "service": CONFIG.name, "version": CONFIG.version}

    def readyz(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(OBBError("engine not initialized"))
        books = self.engine.all_books()
        healthy = [b for b in books if b.health.value == "HEALTHY"]
        body = {
            "status": "ready" if healthy else "building",
            "books_total": len(books),
            "books_healthy": len(healthy),
        }
        return (200 if healthy else 503), body

    # ------------------------------------------------------------------
    # Book introspection
    # ------------------------------------------------------------------

    def books(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(OBBError("engine not initialized"))
        rows = []
        for book in self.engine.all_books():
            tob = book.top_of_book()
            rows.append({
                "symbol": book.canonical_symbol,
                "venue": book.venue_id,
                "health": book.health.value,
                "levels_bid": len(book.bids),
                "levels_ask": len(book.asks),
                "bid_qty": book.total_bid_qty(),
                "ask_qty": book.total_ask_qty(),
                "tob": tob.to_dict(),
                "messages_applied": book.messages_applied,
                "rebuilds": book.rebuilds,
            })
        return 200, {"count": len(rows), "books": rows}

    def book_snapshot(self, symbol: str) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(OBBError("engine not initialized"))
        venues = [b for b in self.engine.all_books() if b.canonical_symbol == symbol]
        if not venues:
            return 404, error_envelope(OBBError(f"no book for {symbol!r}"))
        return 200, {
            "symbol": symbol,
            "venues": [b.to_snapshot_dict(CONFIG.snapshot.snapshot_depth) for b in venues],
        }

    def top_of_book(self, symbol: str) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(OBBError("engine not initialized"))
        venues = [b for b in self.engine.all_books() if b.canonical_symbol == symbol]
        if not venues:
            return 404, error_envelope(OBBError(f"no book for {symbol!r}"))
        return 200, {
            "symbol": symbol,
            "tob": [b.top_of_book(now_ns()).to_dict() | {"venue": b.venue_id} for b in venues],
        }

    # ------------------------------------------------------------------
    # Events / control
    # ------------------------------------------------------------------

    def events(self, limit: int = 100) -> Tuple[int, Dict[str, Any]]:
        evts = self.recent_events[-limit:]
        return 200, {
            "count": len(evts),
            "events": [
                {
                    "kind": e.kind.value,
                    "symbol": e.canonical_symbol,
                    "venue": e.venue_id,
                    "detail": e.detail,
                    "ts": e.timestamp_ns,
                }
                for e in evts
            ],
        }

    def rebuild(self, symbol: str) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None or self.gateway is None:
            return 503, error_envelope(OBBError("not initialized"))
        snap = self.gateway.fetch_snapshot(symbol)
        if snap is None:
            return 502, error_envelope(OBBError(f"no snapshot available for {symbol!r}"))
        venue = snap.get("ven", "primary")
        book = self.engine.rebuild_from_snapshot(
            symbol, venue, snap.get("bids", []), snap.get("asks", [])
        )
        return 200, {
            "status": "rebuilt",
            "symbol": symbol,
            "venue": venue,
            "levels_bid": len(book.bids),
            "levels_ask": len(book.asks),
        }

    def stats(self) -> Tuple[int, Dict[str, Any]]:
        if self.engine is None:
            return 503, error_envelope(OBBError("engine not initialized"))
        return 200, {"engine": dict(self.engine.stats)}
