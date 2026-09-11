"""portfolio_analytics — service-level configuration.

The portfolio analytics service (S9) is the **risk & performance plane** for
the platform's book of positions.  It owns:

* **P&L computation** — realized + unrealized P&L per symbol and in aggregate,
  marked to reference prices pulled from the position-keeper (S6);
* **performance metrics** — rolling return series -> Sharpe ratio, maximum
  drawdown, win rate;
* **risk metrics** — historical parametric VaR / CVaR over the return series;
* **attribution** — decomposition of total P&L into per-symbol realized and
  unrealized contributions.

Upstream is a single service: the position-keeper (S6), which is the
authoritative ledger of positions and realized P&L.  Downstream is the
api-gateway (S15), which proxies these views to external clients.

Configuration is split into four namespaces:

    :class:`IngestConfig`     — polling cadence + S6 endpoint/timeout
    :class:`MetricsConfig`    — return-series window, annualization factors
    :class:`RiskConfig`       — VaR confidence level and method
    :class:`HistoryConfig`    — bounded retention of the equity/return series

All durations use the ``_ms`` suffix and timestamps on hot paths are int64
nanoseconds since the Unix epoch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class IngestConfig:
    """Upstream polling behavior toward S6 (position-keeper)."""

    position_keeper_url: str = "http://position-keeper:7660"
    poll_interval_ms: int = 1_000       # cadence of the refresh loop
    request_timeout_ms: int = 500       # per-call HTTP timeout to S6


@dataclass(frozen=True)
class MetricsConfig:
    """Return-series windowing and annualization factors.

    ``returns_per_year`` is the number of observation periods per year used to
    annualize the Sharpe ratio; it is derived from the polling cadence so the
    metric stays meaningful regardless of how fast the refresh loop runs.
    """

    max_returns: int = 5_000            # bounded rolling window of returns
    risk_free_annual: float = 0.0       # annual risk-free rate (decimal)
    periods_per_day: int = 252 * 24     # default annualization basis (trading days x hours)
    min_returns_for_sharpe: int = 3     # need at least this many returns to report Sharpe


@dataclass(frozen=True)
class RiskConfig:
    """Value-at-Risk parameters."""

    confidence: float = 0.95            # VaR confidence level (0 < c < 1)
    method: str = "historical"          # "historical" | "parametric"


@dataclass(frozen=True)
class HistoryConfig:
    """Bounded retention of the equity and return series."""

    max_history_points: int = 2_000     # bounded rolling window of equity samples
    min_samples_for_var: int = 30       # need at least this many returns for VaR


@dataclass(frozen=True)
class ServiceConfig:
    name: str = "portfolio-analytics"
    version: str = "1.0.0"
    env: str = "production"
    listen_port: int = 7690
    ingest: IngestConfig = field(default_factory=IngestConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)


def validate_config(cfg: ServiceConfig) -> List[str]:
    """Validate the full configuration tree; returns error strings (empty = valid)."""
    errors: List[str] = []
    if not (1 <= cfg.listen_port <= 65535):
        errors.append(f"listen_port out of range: {cfg.listen_port}")

    ing = cfg.ingest
    if not ing.position_keeper_url.startswith(("http://", "https://")):
        errors.append(
            f"ingest.position_keeper_url must be an http(s) URL: {ing.position_keeper_url!r}"
        )
    if ing.poll_interval_ms < 1:
        errors.append("ingest.poll_interval_ms must be >= 1")
    if ing.request_timeout_ms < 1:
        errors.append("ingest.request_timeout_ms must be >= 1")

    mt = cfg.metrics
    if mt.max_returns < 2:
        errors.append("metrics.max_returns must be >= 2")
    if not (-1.0 <= mt.risk_free_annual <= 1.0):
        errors.append(f"metrics.risk_free_annual out of [-1,1]: {mt.risk_free_annual}")
    if mt.periods_per_day < 1:
        errors.append("metrics.periods_per_day must be >= 1")
    if mt.min_returns_for_sharpe < 2:
        errors.append("metrics.min_returns_for_sharpe must be >= 2")

    rk = cfg.risk
    if not (0.5 < rk.confidence < 1.0):
        errors.append(f"risk.confidence out of (0.5,1): {rk.confidence}")
    if rk.method not in ("historical", "parametric"):
        errors.append(f"risk.method must be 'historical' or 'parametric': {rk.method!r}")

    hi = cfg.history
    if hi.max_history_points < 2:
        errors.append("history.max_history_points must be >= 2")
    if hi.min_samples_for_var < 2:
        errors.append("history.min_samples_for_var must be >= 2")

    return errors


# Singleton used by every module in the service.
CONFIG = ServiceConfig()
