"""order_book_builder — service-level configuration.

The order book builder maintains a full L2 limit order book per
(canonical_symbol, venue) pair from the normalized quote stream produced by
the market-data-gateway (S1).  Configuration covers:

    :class:`BookConfig`     — per-book sizing, depth limits, and rebuild policy
    :class:`IngestConfig`   — subscription + backpressure behavior toward S1
    :class:`SnapshotConfig` — snapshot cadence and persistence tuning
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class BookConfig:
    """Per-book structural limits and rebuild policy."""

    max_depth_levels: int = 50                # levels kept per side
    max_levels_per_price: int = 1             # aggregate to one level per price
    book_rebuild_interval_s: float = 30.0     # periodic full resync from S1 snapshot
    stale_book_ttl_ms: int = 2_000            # book with no updates in this window is STALE
    imbalance_alert_ratio: float = 0.85       # bid/ask qty ratio triggering an alert
    min_tick_move_for_rebuild: int = 3        # consecutive top-price moves before forced rebuild check
    orphan_level_grace_ms: int = 500          # DELETE without matching level tolerated this long


@dataclass(frozen=True)
class IngestConfig:
    """Subscription and backpressure behavior toward the market-data-gateway."""

    gateway_url: str = "http://market-data-gateway:7610"
    subscribe_symbols: tuple = (
        # EU index futures
        "EU_STOXX50_CONT", "EU_DAX_CONT", "EU_TECDAX_CONT", "EU_MDAX_CONT",
        # EU rates
        "EU_BUND_CONT", "EU_BUND_NEXT",
        # US equity index futures
        "US_S&P500_CONT", "US_EMINIS_CONT", "US_NASDAQ100_CONT", "US_RUSSELL2K_CONT",
        # energy
        "US_WTI_CRUDE_CONT", "US_NATGAS_CONT", "US_RBOB_CONT", "US_HEATING_OIL_CONT",
        # ags
        "US_CORN_CONT", "US_WHEAT_CONT",
    )
    max_lag_quotes: int = 4096                # consumer lag before backpressure signal
    poll_interval_ms: int = 2
    request_timeout_ms: int = 500


@dataclass(frozen=True)
class SnapshotConfig:
    """Snapshot cadence + persistence tuning."""

    snapshot_interval_s: float = 1.0          # emit book snapshot to consumers at this cadence
    snapshot_depth: int = 20                  # levels included in each snapshot
    persist_top_of_book_every_ms: int = 5     # ToB persistence rate for analytics
    recent_snapshots_kept: int = 600          # rolling in-memory snapshot history


@dataclass(frozen=True)
class ServiceConfig:
    name: str = "order-book-builder"
    version: str = "1.0.0"
    env: str = "production"
    listen_port: int = 7620
    book: BookConfig = field(default_factory=BookConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)


def validate_config(cfg: ServiceConfig) -> List[str]:
    """Validate the full configuration tree; returns error strings."""
    errors: List[str] = []
    if not (1 <= cfg.listen_port <= 65535):
        errors.append(f"listen_port out of range: {cfg.listen_port}")
    if cfg.book.max_depth_levels < 1:
        errors.append("book.max_depth_levels must be >= 1")
    if cfg.book.stale_book_ttl_ms < 10:
        errors.append("book.stale_book_ttl_ms must be >= 10 ms")
    if not (0.0 < cfg.book.imbalance_alert_ratio <= 1.0):
        errors.append("book.imbalance_alert_ratio must be in (0, 1]")
    if cfg.ingest.max_lag_quotes < 64:
        errors.append("ingest.max_lag_quotes must be >= 64")
    if cfg.snapshot.snapshot_depth > cfg.book.max_depth_levels:
        errors.append("snapshot.snapshot_depth cannot exceed book.max_depth_levels")
    if not cfg.ingest.subscribe_symbols:
        errors.append("ingest.subscribe_symbols must not be empty")
    return errors


# Singleton used by every module in the service.
CONFIG = ServiceConfig()
