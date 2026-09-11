"""market_data_gateway — quote normalization pipeline.

The normalizer sits between the raw venue feed and the distribution layer.
Its responsibilities:

1.  Map ``(venue_id, venue_symbol)`` to a canonical symbol using the
    configuration's ``canonical_map``.
2.  Validate the price against the instrument tick grid (off-grid prices are
    flagged — they never crash the pipeline).
3.  Stamp the normalization latency (receive timestamp -> emit timestamp).
4.  Assign an initial :class:`DataQuality` flag based on staleness and
    sequence tracking state; the dedicated quality monitor may later upgrade
    it to ``STALE`` / ``GAP_SUSPECT``.

The normalizer is deliberately allocation-light: every method operates on
plain dataclasses and returns new objects without touching shared mutable
state, so it can be called from any thread/actor in the service.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from .config import CONFIG, NormalizationConfig
from .errors import MissingTickSizeError, NormalizationError, TickSizeViolation, UnknownSymbolError
from .models import (
    DataQuality,
    Quote,
    RawQuote,
    RawTrade,
    SequenceTracker,
    TradePrint,
    now_ns,
)

logger = logging.getLogger("mdg.normalizer")


class QuoteNormalizer:
    """Stateful normalizer holding per-(venue,symbol) sequence trackers.

    The tracker table is the only mutable state; it is keyed by
    ``(venue_id, symbol_venue)`` and updated on every message.  All other
    configuration data (symbol map, tick sizes) is read-only.
    """

    def __init__(self, normalization: NormalizationConfig = CONFIG.normalization) -> None:
        self._normalization = normalization
        self._trackers: Dict[Tuple[str, str], SequenceTracker] = {}
        # counters for observability (read by the metrics exporter)
        self.stats = {
            "normalized_quotes": 0,
            "normalized_trades": 0,
            "off_grid_prices": 0,
            "unknown_symbols_dropped": 0,
            "out_of_order_msgs": 0,
            "gap_events": 0,
        }

    # ------------------------------------------------------------------
    # Sequence tracking
    # ------------------------------------------------------------------

    def _tracker_for(self, venue_id: str, symbol_venue: str) -> SequenceTracker:
        key = (venue_id, symbol_venue)
        tracker = self._trackers.get(key)
        if tracker is None:
            tracker = SequenceTracker(venue_id=venue_id, symbol_venue=symbol_venue)
            self._trackers[key] = tracker
        return tracker

    # ------------------------------------------------------------------
    # Symbol mapping + tick lookup
    # ------------------------------------------------------------------

    def map_symbol(self, venue_id: str, symbol_venue: str) -> str:
        """Map a venue-local symbol to its canonical name.

        Raises :class:`UnknownSymbolError` when the pair is not in the map —
        callers in the hot path catch this and drop the message with a counter
        increment instead of letting it propagate.
        """
        canonical = self._normalization.canonical_map.get((venue_id, symbol_venue))
        if canonical is None:
            raise UnknownSymbolError(venue_id, symbol_venue)
        return canonical

    def tick_size_for(self, canonical_symbol: str) -> float:
        entry = self._normalization.tick_sizes.get(canonical_symbol)
        if entry is None:
            raise MissingTickSizeError(canonical_symbol)
        return entry[0]

    # ------------------------------------------------------------------
    # Tick grid validation
    # ------------------------------------------------------------------

    @staticmethod
    def _is_on_tick_grid(price: float, tick_size: float) -> bool:
        """Check ``price`` lies on the ``tick_size`` grid within 1e-9 relative.

        Floating point division is used with a tolerance because prices are
        decimal-unit floats; the tolerance absorbs binary representation error
        without accepting genuinely off-grid prices.
        """
        if tick_size <= 0.0:
            return True
        quotient = price / tick_size
        nearest = round(quotient)
        return abs(quotient - nearest) < 1e-9

    # ------------------------------------------------------------------
    # Quote normalization (hot path)
    # ------------------------------------------------------------------

    def normalize_quote(self, raw: RawQuote, emit_ts_ns: Optional[int] = None) -> Optional[Quote]:
        """Normalize a raw venue quote into a canonical :class:`Quote`.

        Returns ``None`` when the message must be dropped (unknown symbol).
        Off-grid prices are *not* dropped — they are flagged in quality and
        counted, because venues occasionally emit rounding artifacts during
        auction transitions.
        """
        emit_ts_ns = emit_ts_ns if emit_ts_ns is not None else now_ns()

        # 1) symbol mapping -------------------------------------------------
        try:
            canonical = self.map_symbol(raw.venue_id, raw.symbol_venue)
        except UnknownSymbolError:
            self.stats["unknown_symbols_dropped"] += 1
            logger.debug(
                "dropping quote for unknown symbol venue=%s sym=%s seq=%d",
                raw.venue_id, raw.symbol_venue, raw.seq_no,
            )
            return None

        # 2) tick size lookup (config is validated at boot, so this cannot
        #    raise in practice; guard anyway for defense in depth) ----------
        try:
            tick_size = self.tick_size_for(canonical)
        except MissingTickSizeError:
            logger.error("no tick size for canonical symbol %s", canonical)
            return None

        # 3) tick grid validation -------------------------------------------
        on_grid = self._is_on_tick_grid(raw.price, tick_size)
        if not on_grid:
            self.stats["off_grid_prices"] += 1
            logger.debug(
                "off-grid price sym=%s px=%.6f tick=%.6f seq=%d",
                canonical, raw.price, tick_size, raw.seq_no,
            )

        # 4) sequence tracking ----------------------------------------------
        tracker = self._tracker_for(raw.venue_id, raw.symbol_venue)
        gap = tracker.observe(raw.seq_no, emit_ts_ns)
        if gap is not None and gap < 0:
            self.stats["out_of_order_msgs"] += 1
        elif gap is not None and gap > CONFIG.quality.max_sequence_gap:
            self.stats["gap_events"] += 1

        # 5) initial quality flag -------------------------------------------
        staleness_ms = (emit_ts_ns - raw.venue_timestamp_ns) / 1_000_000.0
        if staleness_ms > CONFIG.quality.max_staleness_ms:
            quality = DataQuality.STALE
        elif staleness_ms > CONFIG.quality.stale_warn_ms:
            quality = DataQuality.STALE_WARN
        else:
            quality = DataQuality.FRESH

        # 6) build the normalized quote -------------------------------------
        latency_ns = emit_ts_ns - raw.receive_timestamp_ns
        if latency_ns < 0:
            # clock regression between receive stamp and emit stamp (should be
            # impossible on a single host); clamp to zero rather than emit a
            # negative latency that would corrupt downstream averages.
            latency_ns = 0

        return Quote(
            canonical_symbol=canonical,
            venue_id=raw.venue_id,
            seq_no=raw.seq_no,
            action=raw.msg_type,
            side=raw.side,
            price=raw.price,
            quantity=raw.quantity,
            depth_level=raw.depth_level,
            tick_size=tick_size,
            quality=quality,
            venue_timestamp_ns=raw.venue_timestamp_ns,
            receive_timestamp_ns=raw.receive_timestamp_ns,
            normalize_latency_ns=latency_ns,
        )

    # ------------------------------------------------------------------
    # Trade normalization
    # ------------------------------------------------------------------

    def normalize_trade(self, raw: RawTrade, emit_ts_ns: Optional[int] = None) -> Optional[TradePrint]:
        """Normalize a raw trade print into a canonical :class:`TradePrint`."""
        emit_ts_ns = emit_ts_ns if emit_ts_ns is not None else now_ns()

        try:
            canonical = self.map_symbol(raw.venue_id, raw.symbol_venue)
        except UnknownSymbolError:
            self.stats["unknown_symbols_dropped"] += 1
            return None

        try:
            tick_size = self.tick_size_for(canonical)
        except MissingTickSizeError:
            logger.error("no tick size for canonical symbol %s", canonical)
            return None

        tracker = self._tracker_for(raw.venue_id, raw.symbol_venue)
        gap = tracker.observe(raw.seq_no, emit_ts_ns)
        if gap is not None and gap < 0:
            self.stats["out_of_order_msgs"] += 1
        elif gap is not None and gap > CONFIG.quality.max_sequence_gap:
            self.stats["gap_events"] += 1

        staleness_ms = (emit_ts_ns - raw.venue_timestamp_ns) / 1_000_000.0
        if staleness_ms > CONFIG.quality.max_staleness_ms:
            quality = DataQuality.STALE
        elif staleness_ms > CONFIG.quality.stale_warn_ms:
            quality = DataQuality.STALE_WARN
        else:
            quality = DataQuality.FRESH

        latency_ns = max(0, emit_ts_ns - raw.receive_timestamp_ns)

        self.stats["normalized_trades"] += 1
        return TradePrint(
            canonical_symbol=canonical,
            venue_id=raw.venue_id,
            seq_no=raw.seq_no,
            price=raw.price,
            quantity=raw.quantity,
            aggressor_side=raw.aggressor_side,
            exec_id=raw.exec_id,
            tick_size=tick_size,
            quality=quality,
            venue_timestamp_ns=raw.venue_timestamp_ns,
            receive_timestamp_ns=raw.receive_timestamp_ns,
            normalize_latency_ns=latency_ns,
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def tracker_snapshot(self) -> Dict[Tuple[str, str], SequenceTracker]:
        """Return a shallow copy of the tracker table (for health endpoints)."""
        return dict(self._trackers)

    def reset_trackers(self) -> None:
        """Drop all sequence state (used after a full snapshot resync)."""
        self._trackers.clear()
