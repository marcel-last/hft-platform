"""position_keeper — service-level configuration.

The position keeper (S6) is the **authoritative** source of real-time position
state per (account, symbol).  It owns:

* **fill ingestion** — every fill reported by the execution gateway (S4) is
  applied exactly once to the authoritative position ledger;
* **manual adjustments & corporate actions** — operator-initiated corrections
  and splits/dividends that alter position quantity or cost basis;
* **position snapshots & history** — point-in-time views for analytics (S9)
  and settlement (S14), plus a bounded per-symbol event history.

Configuration is split into four namespaces:

    :class:`IngestConfig`      — polling behavior toward S4 fills
    :class:`HistoryConfig`     — per-symbol event-history retention
    :class:`SnapshotConfig`    — snapshot cadence + reference-price source
    :class:`AccountingConfig`  — accounting defaults (account id, currency)

All durations use the ``_ms`` suffix and timestamps on hot paths are int64
nanoseconds since the Unix epoch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class IngestConfig:
    """Upstream polling behavior toward S4 (execution-gateway fills)."""

    execution_gateway_url: str = "http://execution-gateway:7640"
    poll_interval_ms: int = 100          # fill-poll cadence for the ingest loop
    request_timeout_ms: int = 500        # per-call HTTP timeout to S4
    max_fills_per_poll: int = 512        # upper bound on fills fetched per poll


@dataclass(frozen=True)
class HistoryConfig:
    """Per-symbol position event history retention."""

    max_events_per_symbol: int = 10_000   # bounded ring of events per symbol
    snapshot_retention: int = 288         # number of periodic snapshots kept


@dataclass(frozen=True)
class SnapshotConfig:
    """Periodic position-snapshot behavior and reference-price source."""

    snapshot_interval_ms: int = 5_000     # cadence for automatic snapshots
    book_builder_url: str = "http://order-book-builder:7620"
    ref_price_timeout_ms: int = 300       # timeout when fetching ToB reference prices


@dataclass(frozen=True)
class AccountingConfig:
    """Accounting defaults applied to the position ledger."""

    default_account: str = "MAIN"         # account id used when a fill omits one
    base_currency: str = "USD"            # platform base currency for notionals
    max_positions: int = 10_000           # guard against unbounded symbol growth


@dataclass(frozen=True)
class ServiceConfig:
    name: str = "position-keeper"
    version: str = "1.0.0"
    env: str = "production"
    listen_port: int = 7660
    ingest: IngestConfig = field(default_factory=IngestConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)
    accounting: AccountingConfig = field(default_factory=AccountingConfig)


def validate_config(cfg: ServiceConfig) -> List[str]:
    """Validate the full configuration tree; returns error strings (empty = valid)."""
    errors: List[str] = []
    if not (1 <= cfg.listen_port <= 65535):
        errors.append(f"listen_port out of range: {cfg.listen_port}")

    ing = cfg.ingest
    if ing.poll_interval_ms < 1:
        errors.append("ingest.poll_interval_ms must be >= 1")
    if ing.request_timeout_ms < 1:
        errors.append("ingest.request_timeout_ms must be >= 1")
    if ing.max_fills_per_poll < 1:
        errors.append("ingest.max_fills_per_poll must be >= 1")
    if not ing.execution_gateway_url.startswith(("http://", "https://")):
        errors.append(f"ingest.execution_gateway_url must be an http(s) URL: {ing.execution_gateway_url!r}")

    hist = cfg.history
    if hist.max_events_per_symbol < 1:
        errors.append("history.max_events_per_symbol must be >= 1")
    if hist.snapshot_retention < 1:
        errors.append("history.snapshot_retention must be >= 1")

    snap = cfg.snapshot
    if snap.snapshot_interval_ms < 1:
        errors.append("snapshot.snapshot_interval_ms must be >= 1")
    if not snap.book_builder_url.startswith(("http://", "https://")):
        errors.append(f"snapshot.book_builder_url must be an http(s) URL: {snap.book_builder_url!r}")
    if snap.ref_price_timeout_ms < 1:
        errors.append("snapshot.ref_price_timeout_ms must be >= 1")

    acc = cfg.accounting
    if not acc.default_account or not acc.default_account.strip():
        errors.append("accounting.default_account must be a non-empty string")
    if not acc.base_currency or len(acc.base_currency) != 3:
        errors.append(f"accounting.base_currency must be a 3-letter code: {acc.base_currency!r}")
    if acc.max_positions < 1:
        errors.append("accounting.max_positions must be >= 1")

    return errors


# Singleton used by every module in the service.
CONFIG = ServiceConfig()
