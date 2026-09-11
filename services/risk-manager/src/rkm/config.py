"""risk_manager — service-level configuration.

The risk manager sits between the strategy engine (S3) / execution gateway
(S4) and the venue boundary.  It owns:

* **pre-trade checks** — every order intent is validated against per-symbol,
  portfolio-wide, and velocity limits before it may reach a venue;
* **real-time exposure tracking** — notional/position state updated from fill
  reports (S4) and book views (S2);
* **breach detection + kill-switch** — hard-limit breaches are recorded,
  escalated downstream, and the optional kill-switch halts all trading.

Configuration is split into four namespaces:

    :class:`LimitsConfig`   — default risk limits (per-symbol / portfolio)
    :class:`VelocityConfig` — order-flow velocity caps (orders per window)
    :class:`KillSwitchConfig` — kill-switch behavior + breach escalation
    :class:`IngestConfig`   — upstream polling toward S2/S4

All monetary values are in the platform base currency; all durations use the
``_ms`` suffix and timestamps on hot paths are int64 nanoseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class LimitsConfig:
    """Default risk limits applied per symbol unless overridden via PUT /limits."""

    max_position_qty: int = 100          # hard cap on |net position| lots per symbol
    max_notional_per_symbol: float = 5_000_000.0   # gross notional cap per symbol
    max_portfolio_notional: float = 25_000_000.0   # aggregate gross notional cap
    max_order_qty: int = 100             # hard cap on a single order's quantity
    max_order_notional: float = 1_000_000.0        # hard cap on a single order's notional
    min_price: float = 0.0               # reject orders priced at/below this (empty-book guard)


@dataclass(frozen=True)
class VelocityConfig:
    """Order-flow velocity caps: max orders per symbol within a sliding window."""

    window_ms: int = 60_000              # sliding window length for velocity counting
    max_orders_per_symbol: int = 250     # hard cap on orders submitted per symbol in the window
    max_orders_total: int = 1_000        # hard cap on total orders across all symbols in the window


@dataclass(frozen=True)
class KillSwitchConfig:
    """Kill-switch behavior and breach escalation."""

    auto_engage_on_hard_breach: bool = False   # engage kill-switch automatically on a HARD breach
    flatten_on_engage: bool = True             # cancel all open orders when engaged
    max_open_orders: int = 500                 # portfolio-wide cap on concurrently open orders
    escalation_url: str = "http://alerting-service:7700"   # S10 alert sink
    audit_url: str = "http://audit-logger:7730"            # S13 audit sink
    escalate_timeout_ms: int = 500            # per-call timeout for downstream escalation


@dataclass(frozen=True)
class IngestConfig:
    """Upstream polling behavior toward S2 (books) and S4 (fills/orders)."""

    book_builder_url: str = "http://order-book-builder:7620"
    execution_gateway_url: str = "http://execution-gateway:7640"
    poll_interval_ms: int = 100              # real-time exposure refresh cadence
    request_timeout_ms: int = 500            # per-call HTTP timeout to S2/S4


@dataclass(frozen=True)
class ServiceConfig:
    name: str = "risk-manager"
    version: str = "1.0.0"
    env: str = "production"
    listen_port: int = 7650
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    velocity: VelocityConfig = field(default_factory=VelocityConfig)
    kill_switch: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)


def validate_config(cfg: ServiceConfig) -> List[str]:
    """Validate the full configuration tree; returns error strings (empty = valid)."""
    errors: List[str] = []
    if not (1 <= cfg.listen_port <= 65535):
        errors.append(f"listen_port out of range: {cfg.listen_port}")

    lim = cfg.limits
    if lim.max_position_qty < 1:
        errors.append("limits.max_position_qty must be >= 1")
    if lim.max_notional_per_symbol <= 0:
        errors.append("limits.max_notional_per_symbol must be > 0")
    if lim.max_portfolio_notional <= 0:
        errors.append("limits.max_portfolio_notional must be > 0")
    if lim.max_order_qty < 1:
        errors.append("limits.max_order_qty must be >= 1")
    if lim.max_order_notional <= 0:
        errors.append("limits.max_order_notional must be > 0")
    if lim.min_price < 0:
        errors.append("limits.min_price must be >= 0")

    vel = cfg.velocity
    if vel.window_ms < 1:
        errors.append("velocity.window_ms must be >= 1")
    if vel.max_orders_per_symbol < 1:
        errors.append("velocity.max_orders_per_symbol must be >= 1")
    if vel.max_orders_total < 1:
        errors.append("velocity.max_orders_total must be >= 1")

    ks = cfg.kill_switch
    if ks.max_open_orders < 1:
        errors.append("kill_switch.max_open_orders must be >= 1")
    if not ks.escalation_url.startswith(("http://", "https://")):
        errors.append(f"kill_switch.escalation_url must be an http(s) URL: {ks.escalation_url!r}")
    if not ks.audit_url.startswith(("http://", "https://")):
        errors.append(f"kill_switch.audit_url must be an http(s) URL: {ks.audit_url!r}")

    ing = cfg.ingest
    if ing.poll_interval_ms < 1:
        errors.append("ingest.poll_interval_ms must be >= 1")
    if ing.request_timeout_ms < 1:
        errors.append("ingest.request_timeout_ms must be >= 1")

    return errors


# Singleton used by every module in the service.
CONFIG = ServiceConfig()
