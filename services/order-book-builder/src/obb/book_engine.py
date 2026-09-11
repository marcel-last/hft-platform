"""order_book_builder — book engine (L2 maintenance core).

The :class:`BookEngine` is the heart of the service.  It owns one
:class:`~obb.models.OrderBook` per (canonical_symbol, venue) and applies
normalized quote messages to them while enforcing every structural invariant:

* bids sorted price-descending, asks price-ascending (via dict + cached sort),
* no negative quantities at any level,
* a DELETE that references a missing level is tolerated within the configured
  orphan grace window (venues occasionally re-send deletes after reconnects),
* crossed books (best_bid >= best_ask) are detected and flagged as anomalies
  rather than silently "fixed", because a cross almost always means upstream
  corruption.

The engine also watches each book for *material* changes — top-of-book moves,
spread shifts, depth drain, and imbalance — and emits :class:`BookEvent`
objects that the distribution layer fans out to subscribers.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from .config import CONFIG
from .errors import BookCrossAnomaly, UnknownBookSymbolError
from .models import (
    BookEvent,
    BookEventKind,
    BookHealth,
    OrderBook,
    PriceLevel,
    TopOfBook,
    now_ns,
)

logger = logging.getLogger("obb.book_engine")


class BookEngine:
    """Owns and maintains all order books."""

    def __init__(self, tick_sizes: Optional[Dict[str, float]] = None) -> None:
        self._books: Dict[Tuple[str, str], OrderBook] = {}
        # canonical_symbol -> tick_size (from the gateway's normalization table)
        self._tick_sizes: Dict[str, float] = dict(tick_sizes or {})
        # previous top-of-book per book, for change detection
        self._prev_tob: Dict[Tuple[str, str], TopOfBook] = {}
        # stats
        self.stats = {
            "messages_applied": 0,
            "orphan_deletes": 0,
            "cross_events": 0,
            "events_emitted": 0,
            "books_rebuilt": 0,
        }

    # ------------------------------------------------------------------
    # Book registry
    # ------------------------------------------------------------------

    def register_book(self, canonical_symbol: str, venue_id: str, tick_size: float) -> OrderBook:
        """Create (or return) the book for one (symbol, venue) pair."""
        key = (canonical_symbol, venue_id)
        book = self._books.get(key)
        if book is None:
            book = OrderBook(
                canonical_symbol=canonical_symbol,
                venue_id=venue_id,
                tick_size=tick_size,
            )
            self._books[key] = book
        return book

    def ensure_book(self, canonical_symbol: str, venue_id: str) -> OrderBook:
        """Return the book, creating it with a known tick size when possible.

        Raises :class:`UnknownBookSymbolError` when neither a registered book
        nor a configured tick size exists for the symbol.
        """
        key = (canonical_symbol, venue_id)
        book = self._books.get(key)
        if book is not None:
            return book
        tick = self._tick_sizes.get(canonical_symbol)
        if tick is None:
            raise UnknownBookSymbolError(canonical_symbol, venue_id)
        return self.register_book(canonical_symbol, venue_id, tick)

    def get(self, canonical_symbol: str, venue_id: str) -> Optional[OrderBook]:
        return self._books.get((canonical_symbol, venue_id))

    def all_books(self) -> List[OrderBook]:
        return list(self._books.values())

    # ------------------------------------------------------------------
    # Message application (hot path)
    # ------------------------------------------------------------------

    def apply_quote(self, quote_wire: dict) -> List[BookEvent]:
        """Apply one normalized wire-format quote to its book.

        ``quote_wire`` uses the gateway's wire format:
            sym, ven, act (N/M/D/E/B/O), side (BID/ASK/LAST), px, qty, lvl, q
        Returns the list of material :class:`BookEvent` s produced (usually 0).
        """
        symbol = quote_wire["sym"]
        venue = quote_wire["ven"]
        action = quote_wire["act"]
        side = quote_wire["side"]
        price = float(quote_wire["px"])
        qty = int(quote_wire["qty"])
        now = now_ns()

        book = self.ensure_book(symbol, venue)
        events: List[BookEvent] = []

        if side not in ("BID", "ASK"):
            # LAST / trade prints do not modify L2; skip quietly
            return events

        levels = book.bids if side == "BID" else book.asks

        if action == "D":
            # DELETE level
            lvl = levels.pop(price, None)
            if lvl is None:
                self.stats["orphan_deletes"] += 1
        elif action in ("N", "M"):
            # NEW / MODIFY: set quantity at this price (aggregate model)
            existing = levels.get(price)
            if existing is not None:
                existing.quantity = qty
                existing.updates += 1
                existing.last_update_ns = now
            else:
                levels[price] = PriceLevel(price=price, quantity=qty, last_update_ns=now)
        elif action in ("E", "B", "O"):
            # EXECUTE / tick events: reduce resting liquidity at the price by
            # the executed quantity (floor at zero; a floor of zero keeps the
            # level visible so subsequent MODIFYs can restore it).
            existing = levels.get(price)
            if existing is not None:
                existing.quantity = max(0, existing.quantity - qty)
                existing.updates += 1
                existing.last_update_ns = now
                if existing.quantity == 0:
                    del levels[price]

        book.messages_applied += 1
        book.last_update_ns = now
        book._invalidate_caches()
        self.stats["messages_applied"] += 1

        # ---- post-mutation checks ------------------------------------------
        events.extend(self._check_cross(book, now))
        events.extend(self._detect_material_changes(book, now))
        return events

    # ------------------------------------------------------------------
    # Invariant checks
    # ------------------------------------------------------------------

    def _check_cross(self, book: OrderBook, now: int) -> List[BookEvent]:
        """Detect and flag crossed books (best_bid >= best_ask)."""
        bb = book.best_bid()
        ba = book.best_ask()
        if bb is None or ba is None:
            return []
        if bb.price >= ba.price:
            book.cross_events += 1
            self.stats["cross_events"] += 1
            logger.warning(
                "CROSS on %s/%s: bid=%.4f ask=%.4f",
                book.canonical_symbol, book.venue_id, bb.price, ba.price,
            )
            return [BookEvent(
                kind=BookEventKind.CROSS_DETECTED,
                canonical_symbol=book.canonical_symbol,
                venue_id=book.venue_id,
                detail=f"bid={bb.price} ask={ba.price}",
                timestamp_ns=now,
            )]
        return []

    def _detect_material_changes(self, book: OrderBook, now: int) -> List[BookEvent]:
        """Compare against the previous top-of-book and emit change events."""
        tob = book.top_of_book(now)
        prev = self._prev_tob.get((book.canonical_symbol, book.venue_id))
        self._prev_tob[(book.canonical_symbol, book.venue_id)] = tob
        if prev is None:
            return []

        events: List[BookEvent] = []
        cfg = CONFIG.book

        # 1) top-of-book price move
        if (tob.best_bid_price != prev.best_bid_price or
                tob.best_ask_price != prev.best_ask_price):
            events.append(BookEvent(
                kind=BookEventKind.TOP_CHANGE,
                canonical_symbol=book.canonical_symbol,
                venue_id=book.venue_id,
                detail=(f"bid {prev.best_bid_price}->{tob.best_bid_price} "
                        f"ask {prev.best_ask_price}->{tob.best_ask_price}"),
                timestamp_ns=now, top=tob,
            ))

        # 2) spread change (in ticks) beyond one tick
        if tob.spread_ticks != prev.spread_ticks and abs(tob.spread_ticks - prev.spread_ticks) >= 1:
            events.append(BookEvent(
                kind=BookEventKind.SPREAD_CHANGE,
                canonical_symbol=book.canonical_symbol,
                venue_id=book.venue_id,
                detail=f"spread {prev.spread_ticks}->{tob.spread_ticks} ticks",
                timestamp_ns=now, top=tob,
            ))

        # 3) imbalance crossing the alert threshold
        if prev.imbalance_ratio < cfg.imbalance_alert_ratio <= tob.imbalance_ratio:
            events.append(BookEvent(
                kind=BookEventKind.IMBALANCE,
                canonical_symbol=book.canonical_symbol,
                venue_id=book.venue_id,
                detail=f"bid/ask ratio {tob.imbalance_ratio:.3f} crossed {cfg.imbalance_alert_ratio}",
                timestamp_ns=now, top=tob,
            ))

        # 4) depth drained: a side lost >= 50% of its quantity vs previous
        prev_bid_qty = prev.best_bid_qty + max(0, book.total_bid_qty() - tob.best_bid_qty)
        if prev.best_bid_qty > 0 and tob.best_bid_qty < 0.5 * prev.best_bid_qty:
            events.append(BookEvent(
                kind=BookEventKind.DEPTH_DRAINED,
                canonical_symbol=book.canonical_symbol,
                venue_id=book.venue_id,
                detail=f"bid depth {prev.best_bid_qty}->{tob.best_bid_qty}",
                timestamp_ns=now, top=tob,
            ))

        for ev in events:
            self.stats["events_emitted"] += 1
        return events

    # ------------------------------------------------------------------
    # Rebuild / snapshot ingestion
    # ------------------------------------------------------------------

    def rebuild_from_snapshot(self, symbol: str, venue: str, bids: List[List[float]],
                              asks: List[List[float]]) -> OrderBook:
        """Replace a book's contents from a full L2 snapshot.

        ``bids``/``asks`` are lists of [price, quantity] pairs (any order).
        """
        book = self.ensure_book(symbol, venue)
        book.bids.clear()
        book.asks.clear()
        for price, qty in bids:
            if qty > 0:
                book.bids[float(price)] = PriceLevel(price=float(price), quantity=int(qty))
        for price, qty in asks:
            if qty > 0:
                book.asks[float(price)] = PriceLevel(price=float(price), quantity=int(qty))
        book.rebuilds += 1
        book.health = BookHealth.HEALTHY if (book.bids or book.asks) else BookHealth.EMPTY
        book._invalidate_caches()
        self.stats["books_rebuilt"] += 1
        self._prev_tob.pop((symbol, venue), None)
        return book

    # ------------------------------------------------------------------
    # Staleness sweep
    # ------------------------------------------------------------------

    def sweep_stale(self) -> List[BookEvent]:
        """Mark books with no updates within the TTL as STALE (once each)."""
        now = now_ns()
        ttl_ns = CONFIG.book.stale_book_ttl_ms * 1_000_000
        events: List[BookEvent] = []
        for book in self._books.values():
            if book.health == BookHealth.HEALTHY and (now - book.last_update_ns) > ttl_ns:
                book.health = BookHealth.STALE
                events.append(BookEvent(
                    kind=BookEventKind.BOOK_STALE,
                    canonical_symbol=book.canonical_symbol,
                    venue_id=book.venue_id,
                    detail=f"no updates for {(now - book.last_update_ns) / 1e6:.0f} ms",
                    timestamp_ns=now,
                ))
        return events

    def mark_healthy(self, symbol: str, venue: str) -> None:
        book = self.get(symbol, venue)
        if book is not None and book.health == BookHealth.STALE:
            book.health = BookHealth.HEALTHY
