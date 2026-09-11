"""strategy_engine — service-level configuration.

The strategy engine consumes normalized quotes (S1) and book events / top-of-book
views (S2), runs a set of pluggable signal strategies per symbol, and emits
order intents that are pushed to the execution gateway (S4).  Configuration is
split into four namespaces:

    :class:`IngestConfig`   — upstream subscriptions + poll behavior toward S1/S2
    :class:`SignalConfig`   — per-signal throttling, sizing, and staleness rules
    :class:`StrategyConfig` — default parameter sets for each strategy family
    :class:`EmitConfig`     — order-intent emission (risk caps + push to S4)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class IngestConfig:
    """Upstream subscriptions and polling behavior toward S1 and S2."""

    gateway_url: str = "http://market-data-gateway:7610"
    book_builder_url: str = "http://order-book-builder:7620"
    subscribe_symbols: tuple = (
        # EU index futures
        "EU_STOXX50_CONT", "EU_DAX_CONT", "EU_TECDAX_CONT", "EU_MDAX_CONT",
        # US equity index futures
        "US_S&P500_CONT", "US_EMINIS_CONT", "US_NASDAQ100_CONT", "US_RUSSELL2K_CONT",
        # energy
        "US_WTI_CRUDE_CONT", "US_NATGAS_CONT",
    )
    poll_interval_ms: int = 5              # main ingest loop cadence
    request_timeout_ms: int = 500          # per-call HTTP timeout to S1/S2
    quote_poll_limit: int = 256            # max quotes pulled per symbol per poll
    event_poll_limit: int = 512            # max book events pulled per poll
    stale_signal_ttl_ms: int = 1_000       # a signal older than this is never emitted


@dataclass(frozen=True)
class SignalConfig:
    """Per-signal throttling, sizing, and staleness rules."""

    cooldown_ms: int = 250                 # min time between signals for one (symbol, strategy)
    max_open_signals_per_symbol: int = 4   # live signals kept per symbol before oldest is dropped
    default_order_qty: int = 1             # lots per order intent when a strategy does not size
    max_order_qty: int = 50                # hard cap on any single order intent quantity
    min_price: float = 0.0                 # reject signals with px <= this (empty-book guard)


@dataclass(frozen=True)
class MomentumParams:
    """Parameters for the momentum strategy."""

    window_ticks: int = 16                 # mid-price samples in the momentum window
    entry_threshold_ticks: float = 3.0     # |mid change| over window (ticks) to fire
    exit_threshold_ticks: float = 2.0      # adverse move that closes a position
    max_position_qty: int = 10             # max net lots held by this strategy per symbol


@dataclass(frozen=True)
class MeanReversionParams:
    """Parameters for the mean-reversion strategy."""

    window_ticks: int = 32                 # mid-price samples in the deviation window
    z_entry_threshold: float = 2.0         # |z-score| of mid vs window mean to enter
    z_exit_threshold: float = 0.5          # reversion toward the mean that closes a position
    max_position_qty: int = 8              # max net lots held by this strategy per symbol


@dataclass(frozen=True)
class SpreadArbParams:
    """Parameters for the spread-arbitrage strategy (book-structure driven)."""

    imbalance_entry_ratio: float = 2.0     # bid/ask top-level ratio to enter a fade
    spread_max_ticks: int = 3              # ignore books wider than this (no liquidity)
    exit_spread_ticks: int = 1             # take profit when the spread tightens to this
    max_position_qty: int = 6              # max net lots held by this strategy per symbol


@dataclass(frozen=True)
class StrategyConfig:
    """Default parameter sets for each built-in strategy family."""

    momentum: MomentumParams = field(default_factory=MomentumParams)
    mean_reversion: MeanReversionParams = field(default_factory=MeanReversionParams)
    spread_arb: SpreadArbParams = field(default_factory=SpreadArbParams)


@dataclass(frozen=True)
class EmitConfig:
    """Order-intent emission: risk caps and push behavior toward S4."""

    execution_gateway_url: str = "http://execution-gateway:7640"
    max_notional_per_signal: float = 250_000.0   # notional cap per single order intent
    max_open_intents: int = 32                  # live intents kept in the rolling buffer
    push_on_emit: bool = True                   # immediately POST each intent to S4
    request_timeout_ms: int = 500               # timeout for pushes to S4


@dataclass(frozen=True)
class ServiceConfig:
    name: str = "strategy-engine"
    version: str = "1.0.0"
    env: str = "production"
    listen_port: int = 7630
    ingest: IngestConfig = field(default_factory=IngestConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    emit: EmitConfig = field(default_factory=EmitConfig)


def validate_config(cfg: ServiceConfig) -> List[str]:
    """Validate the full configuration tree; returns error strings."""
    errors: List[str] = []
    if not (1 <= cfg.listen_port <= 65535):
        errors.append(f"listen_port out of range: {cfg.listen_port}")
    if cfg.ingest.poll_interval_ms < 1:
        errors.append("ingest.poll_interval_ms must be >= 1")
    if not cfg.ingest.subscribe_symbols:
        errors.append("ingest.subscribe_symbols must not be empty")
    if cfg.signal.cooldown_ms < 0:
        errors.append("signal.cooldown_ms must be >= 0")
    if cfg.signal.max_open_signals_per_symbol < 1:
        errors.append("signal.max_open_signals_per_symbol must be >= 1")
    if not (1 <= cfg.signal.default_order_qty <= cfg.signal.max_order_qty):
        errors.append("signal.default_order_qty must be in [1, max_order_qty]")
    m = cfg.strategy.momentum
    if m.window_ticks < 2:
        errors.append("strategy.momentum.window_ticks must be >= 2")
    if m.entry_threshold_ticks <= 0:
        errors.append("strategy.momentum.entry_threshold_ticks must be > 0")
    r = cfg.strategy.mean_reversion
    if r.window_ticks < 3:
        errors.append("strategy.mean_reversion.window_ticks must be >= 3")
    if r.z_entry_threshold <= 0:
        errors.append("strategy.mean_reversion.z_entry_threshold must be > 0")
    s = cfg.strategy.spread_arb
    if s.imbalance_entry_ratio <= 1.0:
        errors.append("strategy.spread_arb.imbalance_entry_ratio must be > 1.0")
    if s.spread_max_ticks < 1:
        errors.append("strategy.spread_arb.spread_max_ticks must be >= 1")
    if cfg.emit.max_open_intents < 1:
        errors.append("emit.max_open_intents must be >= 1")
    if cfg.emit.max_notional_per_signal <= 0:
        errors.append("emit.max_notional_per_signal must be > 0")
    return errors


# Singleton used by every module in the service.
CONFIG = ServiceConfig()
