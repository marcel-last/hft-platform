"""data_quality_monitor — service-level configuration.

The data quality monitor (S8) is the **unified observability plane** for
market-data health across the platform.  It owns:

* **upstream aggregation** — periodic pulls of per-symbol quality metrics from
  the market-data gateway (S1, staleness / gaps / ordering violations) and book
  health from the order-book-builder (S2, cross events / stale books);
* **degradation detection** — a composite quality score per symbol with
  hysteresis so transient blips do not flap the alert state;
* **degradation alerts** — bounded history of degradation episodes plus best-
  effort fan-out to the alerting service (S10).

Configuration is split into four namespaces:

    :class:`IngestConfig`     — polling cadence + upstream endpoints/timeouts
    :class:`ThresholdsConfig` — staleness / gap / stale-book budgets and weights
    :class:`ScoringConfig`    — composite score thresholds and hysteresis band
    :class:`AlertingConfig`   — alert history retention + S10 fan-out

All durations use the ``_ms`` suffix and timestamps on hot paths are int64
nanoseconds since the Unix epoch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class IngestConfig:
    """Upstream polling behavior toward S1 (gateway) and S2 (book builder)."""

    market_data_gateway_url: str = "http://market-data-gateway:7610"
    order_book_builder_url: str = "http://order-book-builder:7620"
    poll_interval_ms: int = 500        # cadence of the aggregation loop
    request_timeout_ms: int = 500      # per-call HTTP timeout to S1 / S2


@dataclass(frozen=True)
class ThresholdsConfig:
    """Per-metric budgets used by both the composite score and the gap/staleness views."""

    stale_pct_warn: float = 5.0        # % of quotes tagged STALE that starts penalizing
    stale_pct_crit: float = 25.0       # % at which staleness is treated as critical
    max_silent_ms: int = 1_000         # symbol silent for this long => feed gap
    max_staleness_ms: int = 500        # per-quote venue->receive age budget (S1 mirror)
    stale_book_ttl_ms: int = 2_000     # book with no updates this long => STALE health
    cross_event_budget: int = 3        # cross events per window tolerated before penalty
    gap_weight: float = 4.0            # weight of a feed-gap (silence) event in the score
    stale_pct_weight: float = 2.0      # weight multiplier applied to stale_pct in the score


@dataclass(frozen=True)
class ScoringConfig:
    """Composite quality-score thresholds and hysteresis band.

    The composite score is an integer in ``[0, 100]`` (100 = perfect).  A
    symbol degrades when its score falls strictly below ``degraded_below`` and
    recovers only once it climbs back to at least ``recovered_at_or_above``, so
    scores oscillating around the boundary do not flap the alert state.
    """

    degraded_below: int = 70           # score < this => DEGRADED
    recovered_at_or_above: int = 85    # score >= this while DEGRADED => OK again


@dataclass(frozen=True)
class AlertingConfig:
    """Degradation-alert retention and best-effort fan-out to S10."""

    max_degradations: int = 512        # bounded ring of degradation episodes
    alert_service_url: str = "http://alerting-service:7700"
    alert_timeout_ms: int = 300        # timeout for the fire-and-forget S10 POST
    send_alerts: bool = True           # master switch for S10 fan-out


@dataclass(frozen=True)
class ServiceConfig:
    name: str = "data-quality-monitor"
    version: str = "1.0.0"
    env: str = "production"
    listen_port: int = 7680
    ingest: IngestConfig = field(default_factory=IngestConfig)
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    alerting: AlertingConfig = field(default_factory=AlertingConfig)


def validate_config(cfg: ServiceConfig) -> List[str]:
    """Validate the full configuration tree; returns error strings (empty = valid)."""
    errors: List[str] = []
    if not (1 <= cfg.listen_port <= 65535):
        errors.append(f"listen_port out of range: {cfg.listen_port}")

    ing = cfg.ingest
    for label, url in (
        ("ingest.market_data_gateway_url", ing.market_data_gateway_url),
        ("ingest.order_book_builder_url", ing.order_book_builder_url),
    ):
        if not url.startswith(("http://", "https://")):
            errors.append(f"{label} must be an http(s) URL: {url!r}")
    if ing.poll_interval_ms < 1:
        errors.append("ingest.poll_interval_ms must be >= 1")
    if ing.request_timeout_ms < 1:
        errors.append("ingest.request_timeout_ms must be >= 1")

    th = cfg.thresholds
    if not (0.0 <= th.stale_pct_warn <= 100.0):
        errors.append(f"thresholds.stale_pct_warn out of [0,100]: {th.stale_pct_warn}")
    if not (0.0 < th.stale_pct_crit <= 100.0):
        errors.append(f"thresholds.stale_pct_crit must be in (0,100]: {th.stale_pct_crit}")
    if th.stale_pct_warn > th.stale_pct_crit:
        errors.append("thresholds.stale_pct_warn must be <= thresholds.stale_pct_crit")
    if th.max_silent_ms < 1:
        errors.append("thresholds.max_silent_ms must be >= 1")
    if th.max_staleness_ms < 1:
        errors.append("thresholds.max_staleness_ms must be >= 1")
    if th.stale_book_ttl_ms < 1:
        errors.append("thresholds.stale_book_ttl_ms must be >= 1")
    if th.cross_event_budget < 0:
        errors.append("thresholds.cross_event_budget must be >= 0")
    if th.gap_weight <= 0.0:
        errors.append("thresholds.gap_weight must be > 0")
    if th.stale_pct_weight <= 0.0:
        errors.append("thresholds.stale_pct_weight must be > 0")

    sc = cfg.scoring
    if not (0 <= sc.degraded_below < 100):
        errors.append(f"scoring.degraded_below out of [0,100): {sc.degraded_below}")
    if not (0 < sc.recovered_at_or_above <= 100):
        errors.append(f"scoring.recovered_at_or_above out of (0,100]: {sc.recovered_at_or_above}")
    if sc.degraded_below >= sc.recovered_at_or_above:
        errors.append(
            "scoring.degraded_below must be < scoring.recovered_at_or_above "
            "(hysteresis band)"
        )

    al = cfg.alerting
    if al.max_degradations < 1:
        errors.append("alerting.max_degradations must be >= 1")
    if not al.alert_service_url.startswith(("http://", "https://")):
        errors.append(f"alerting.alert_service_url must be an http(s) URL: {al.alert_service_url!r}")
    if al.alert_timeout_ms < 1:
        errors.append("alerting.alert_timeout_ms must be >= 1")

    return errors


# Singleton used by every module in the service.
CONFIG = ServiceConfig()
