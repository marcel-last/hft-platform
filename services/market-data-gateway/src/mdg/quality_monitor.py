"""market_data_gateway — data quality monitor.

The quality monitor watches the normalized quote stream and maintains rolling
per-symbol :class:`~mdg.models.QualityMetrics`.  It is responsible for:

* detecting staleness breaches (and emitting ``STALENESS_BREACH`` control
  events when a symbol goes silent past the hard budget),
* aggregating sequence-gap and out-of-order statistics from the normalizer's
  tracker table,
* producing the periodic quality report consumed by the data-quality-monitor
  service (S8) over the internal API.

The monitor runs on its own thread at a fixed cadence; quote ingestion into
the rolling window happens inline in :meth:`observe` which is called from the
hot path after normalization.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from .config import CONFIG
from .models import (
    ControlEvent,
    ControlEventKind,
    DataQuality,
    Quote,
    QualityMetrics,
    now_ns,
)

logger = logging.getLogger("mdg.quality_monitor")


class _SymbolWindow:
    """Bounded per-symbol observation window."""

    __slots__ = ("metrics", "recent_quotes", "last_seen_ns", "silent_since_breach")

    def __init__(self, canonical_symbol: str, window_start_ns: int) -> None:
        self.metrics = QualityMetrics(canonical_symbol=canonical_symbol, window_start_ns=window_start_ns)
        # keep the last N quote timestamps for staleness sampling
        self.recent_quotes: Deque[int] = deque(maxlen=CONFIG.buffering.drain_batch_size)
        self.last_seen_ns: int = window_start_ns
        self.silent_since_breach: bool = False


class QualityMonitor:
    """Rolling quality monitor over the normalized quote stream."""

    def __init__(self, window_seconds: float = 10.0) -> None:
        self._window_seconds = window_seconds
        self._windows: Dict[str, _SymbolWindow] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # lifetime counters
        self.staleness_breaches_total = 0
        self.symbols_tracked = 0

    # ------------------------------------------------------------------
    # Hot-path observation
    # ------------------------------------------------------------------

    def observe(self, quote: Quote) -> None:
        """Record one normalized quote into the rolling window."""
        now = now_ns()
        with self._lock:
            window = self._windows.get(quote.canonical_symbol)
            if window is None:
                window = _SymbolWindow(quote.canonical_symbol, now)
                self._windows[quote.canonical_symbol] = window
                self.symbols_tracked += 1
            staleness_ms = (now - quote.venue_timestamp_ns) / 1_000_000.0
            window.metrics.record(quote, staleness_ms)
            window.recent_quotes.append(quote.receive_timestamp_ns)
            window.last_seen_ns = now
            if window.silent_since_breach:
                window.silent_since_breach = False

    # ------------------------------------------------------------------
    # Background sweep thread
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._sweep_loop, name="mdg-quality-sweep", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _sweep_loop(self) -> None:
        """Periodically check for silent symbols and rotate windows."""
        sweep_interval_s = 1.0
        while not self._stop_event.wait(sweep_interval_s):
            try:
                self.sweep()
            except Exception:  # pragma: no cover - defensive
                logger.exception("quality monitor sweep failed")

    def sweep(self) -> List[ControlEvent]:
        """One monitoring pass.  Returns control events for silent symbols."""
        now = now_ns()
        events: List[ControlEvent] = []
        window_budget_ns = int(self._window_seconds * 1_000_000_000)
        with self._lock:
            for symbol, window in list(self._windows.items()):
                silent_ms = (now - window.last_seen_ns) / 1_000_000.0
                if silent_ms > CONFIG.quality.max_staleness_ms and not window.silent_since_breach:
                    window.silent_since_breach = True
                    self.staleness_breaches_total += 1
                    events.append(ControlEvent(
                        kind=ControlEventKind.STALENESS_BREACH,
                        venue_id="gateway",
                        detail=f"symbol {symbol} silent for {silent_ms:.0f} ms",
                    ))
                # rotate the window when it is older than the configured span
                if now - window.metrics.window_start_ns > window_budget_ns:
                    window.metrics = QualityMetrics(
                        canonical_symbol=symbol, window_start_ns=now
                    )
        return events

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def report(self) -> Dict[str, dict]:
        """Build the JSON-serializable quality report (for the internal API)."""
        now = now_ns()
        out: Dict[str, dict] = {}
        with self._lock:
            for symbol, window in self._windows.items():
                m = window.metrics
                silent_ms = (now - window.last_seen_ns) / 1_000_000.0
                out[symbol] = {
                    "quotes_seen": m.quotes_seen,
                    "stale_quotes": m.stale_quotes,
                    "stale_pct": round(m.staleness_pct(), 4),
                    "gap_events": m.gap_events,
                    "out_of_order_events": m.out_of_order_events,
                    "max_staleness_ms_observed": round(m.max_staleness_ms_observed, 3),
                    "avg_normalize_latency_ns": round(m.avg_normalize_latency_ns, 1),
                    "silent_ms": round(silent_ms, 1),
                    "window_start_ns": m.window_start_ns,
                }
        return out

    def reset(self) -> None:
        """Clear all windows (used after a full snapshot resync)."""
        with self._lock:
            self._windows.clear()
            self.symbols_tracked = 0
