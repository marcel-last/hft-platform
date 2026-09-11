"""market_data_gateway — service-level configuration.

Every tunable knob of the gateway is declared here as an explicit constant so
that no magic number ever appears in business logic.  The values below are the
production reference values for the EU-West-1 deployment (venue cluster
"EUREX + ICE + CME" hybrid feed).  They are intentionally verbose: each block
is documented, typed, and validated by ``validate_config`` at boot time.

The configuration is split into the following namespaces:

    :class:`NetworkConfig`      — socket / transport tuning
    :class:`FeedConfig`         — per-venue feed subscriptions
    :class:`NormalizationConfig`— symbol map + tick size tables
    :class:`QualityConfig`      — staleness, gap and ordering thresholds
    :class:`BufferingConfig`    — ring buffer sizing for the hot path
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Network / transport tuning
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NetworkConfig:
    """Socket and transport layer tuning knobs."""

    listen_host: str = "0.0.0.0"
    listen_port: int = 7610
    max_connections: int = 256
    recv_buffer_bytes: int = 4 * 1024 * 1024          # 4 MiB kernel rcvbuf
    send_buffer_bytes: int = 8 * 1024 * 1024          # 8 MiB kernel sndbuf
    keepalive_idle_seconds: int = 15
    keepalive_interval_seconds: int = 5
    keepalive_probes: int = 3
    tcp_nodelay: bool = True
    epoll_wait_timeout_ms: int = 2                    # busy-ish polling window
    max_events_per_epoll_wait: int = 1024
    handshake_timeout_ms: int = 250
    reconnect_base_delay_ms: int = 50
    reconnect_max_delay_ms: int = 5_000
    reconnect_jitter_pct: int = 20                    # ±20 % jitter on backoff
    session_idle_disconnect_ms: int = 30_000          # drop silent consumers


# ---------------------------------------------------------------------------
# Per-venue feed subscriptions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VenueFeed:
    """A single venue connection descriptor."""

    venue_id: str
    host: str
    port: int
    protocol: str                                     # "FIX44" | "ITCH" | "MDP3"
    username: str
    password_ref: str                                 # secret manager key, never inline
    symbols: Tuple[str, ...] = ()
    priority: int = 10                                # lower number = higher prio
    failover_group: str = "default"


@dataclass(frozen=True)
class FeedConfig:
    """Aggregate feed subscription table."""

    venues: Tuple[VenueFeed, ...] = (
        VenueFeed(
            venue_id="EUREX",
            host="feed1.eurex.example",
            port=9002,
            protocol="ITCH",
            username="mdg-eu-01",
            password_ref="vault://hft/mdg/eurex-itch-pw",
            symbols=(
                "FESX", "FESM", "FESB", "FESV", "FBUX", "FBUM", "FBUB",
                "FAXX", "FAXD", "FDAX", "FTEC", "FSM1", "FOEC", "FOE2",
            ),
            priority=1,
            failover_group="eurex-primary",
        ),
        VenueFeed(
            venue_id="ICE-EU",
            host="feed.iceeu.example",
            port=8443,
            protocol="FIX44",
            username="mdg-ice-01",
            password_ref="vault://hft/mdg/ice-fix-pw",
            symbols=(
                "ES", "ESM", "EM", "EMM", "NQ", "NQM", "YM", "YMM",
                "CL", "CLF", "NG", "NGF", "RB", "RBF",
            ),
            priority=2,
            failover_group="ice-primary",
        ),
        VenueFeed(
            venue_id="CME-GLOBEX",
            host="mdp3.cme.example",
            port=7411,
            protocol="MDP3",
            username="mdg-cme-01",
            password_ref="vault://hft/mdg/cme-mdp3-pw",
            symbols=(
                "ES", "NQ", "YM", "RTY", "CL", "NG", "RB", "HO", "ZC",
                "ZW", "ZR", "HE",
            ),
            priority=3,
            failover_group="cme-primary",
        ),
    )


# ---------------------------------------------------------------------------
# Normalization tables (symbol map + tick sizes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NormalizationConfig:
    """Canonical symbol mapping and instrument metadata tables.

    ``canonical_map`` maps ``(venue, venue_symbol) -> canonical_symbol``.
    ``tick_sizes`` maps ``canonical_symbol -> (tick_size, price_precision, qty_precision)``.
    """

    canonical_map: Dict[Tuple[str, str], str] = field(default_factory=lambda: {
        # ---- EUREX ------------------------------------------------------
        ("EUREX", "FESX"):  "EU_STOXX50_CONT",
        ("EUREX", "FESM"):  "EU_STOXX50_NEXT",
        ("EUREX", "FESB"):  "EU_STOXX50_B2",
        ("EUREX", "FESV"):  "EU_STOXX50_VOL_CONT",
        ("EUREX", "FBUX"):  "EU_BUND_CONT",
        ("EUREX", "FBUM"):  "EU_BUND_NEXT",
        ("EUREX", "FBUB"):  "EU_BUND_B2",
        ("EUREX", "FAXX"):  "EU_AGRI_CONT",
        ("EUREX", "FAXD"):  "EU_AGRI_DUR_CONT",
        ("EUREX", "FDAX"):  "EU_DAX_CONT",
        ("EUREX", "FTEC"):  "EU_TECDAX_CONT",
        ("EUREX", "FSM1"):  "EU_MDAX_CONT",
        ("EUREX", "FOEC"):  "EU_CO2_CONT",
        ("EUREX", "FOE2"):  "EU_CO2_NEXT",
        # ---- ICE-EU -----------------------------------------------------
        ("ICE-EU", "ES"):   "US_S&P500_CONT",
        ("ICE-EU", "ESM"):  "US_S&P500_NEXT",
        ("ICE-EU", "EM"):   "US_EMINIS_CONT",
        ("ICE-EU", "EMM"):  "US_EMINIS_NEXT",
        ("ICE-EU", "NQ"):   "US_NASDAQ100_CONT",
        ("ICE-EU", "NQM"):  "US_NASDAQ100_NEXT",
        ("ICE-EU", "YM"):   "US_RUSSELL2K_CONT",
        ("ICE-EU", "YMM"):  "US_RUSSELL2K_NEXT",
        ("ICE-EU", "CL"):   "US_WTI_CRUDE_CONT",
        ("ICE-EU", "CLF"):  "US_WTI_CRUDE_NEXT",
        ("ICE-EU", "NG"):   "US_NATGAS_CONT",
        ("ICE-EU", "NGF"):  "US_NATGAS_NEXT",
        ("ICE-EU", "RB"):   "US_RBOB_CONT",
        ("ICE-EU", "RBF"):  "US_RBOB_NEXT",
        # ---- CME-GLOBEX -------------------------------------------------
        ("CME-GLOBEX", "ES"):  "US_S&P500_CONT",
        ("CME-GLOBEX", "NQ"):  "US_NASDAQ100_CONT",
        ("CME-GLOBEX", "YM"):  "US_RUSSELL2K_CONT",
        ("CME-GLOBEX", "RTY"): "US_RUSSELL1K_CONT",
        ("CME-GLOBEX", "CL"):  "US_WTI_CRUDE_CONT",
        ("CME-GLOBEX", "NG"):  "US_NATGAS_CONT",
        ("CME-GLOBEX", "RB"):  "US_RBOB_CONT",
        ("CME-GLOBEX", "HO"):  "US_HEATING_OIL_CONT",
        ("CME-GLOBEX", "ZC"):  "US_CORN_CONT",
        ("CME-GLOBEX", "ZW"):  "US_WHEAT_CONT",
        ("CME-GLOBEX", "ZR"):  "US_RICE_CONT",
        ("CME-GLOBEX", "HE"):  "US_HEAT_EXCHG_CONT",
    })

    tick_sizes: Dict[str, Tuple[float, int, int]] = field(default_factory=lambda: {
        # symbol -> (tick_size, price_decimals, qty_decimals)
        "EU_STOXX50_CONT":   (1.0, 2, 0),
        "EU_STOXX50_NEXT":   (1.0, 2, 0),
        "EU_STOXX50_B2":     (1.0, 2, 0),
        "EU_STOXX50_VOL_CONT": (0.01, 3, 0),
        "EU_BUND_CONT":      (0.005, 4, 0),
        "EU_BUND_NEXT":      (0.005, 4, 0),
        "EU_BUND_B2":        (0.005, 4, 0),
        "EU_AGRI_CONT":      (1.0, 2, 0),
        "EU_AGRI_DUR_CONT":  (1.0, 2, 0),
        "EU_DAX_CONT":       (1.0, 2, 0),
        "EU_TECDAX_CONT":    (1.0, 2, 0),
        "EU_MDAX_CONT":      (1.0, 2, 0),
        "EU_CO2_CONT":       (0.05, 3, 0),
        "EU_CO2_NEXT":       (0.05, 3, 0),
        "US_S&P500_CONT":    (0.25, 4, 0),
        "US_S&P500_NEXT":    (0.25, 4, 0),
        "US_EMINIS_CONT":    (0.25, 4, 0),
        "US_EMINIS_NEXT":    (0.25, 4, 0),
        "US_NASDAQ100_CONT": (0.25, 4, 0),
        "US_NASDAQ100_NEXT": (0.25, 4, 0),
        "US_RUSSELL2K_CONT": (1.0, 4, 0),
        "US_RUSSELL2K_NEXT": (1.0, 4, 0),
        "US_WTI_CRUDE_CONT": (0.01, 4, 0),
        "US_WTI_CRUDE_NEXT": (0.01, 4, 0),
        "US_NATGAS_CONT":    (0.001, 4, 0),
        "US_NATGAS_NEXT":    (0.001, 4, 0),
        "US_RBOB_CONT":      (0.0001, 5, 0),
        "US_RBOB_NEXT":      (0.0001, 5, 0),
        "US_RUSSELL1K_CONT": (0.25, 4, 0),
        "US_HEATING_OIL_CONT": (0.01, 4, 0),
        "US_CORN_CONT":      (0.0025, 4, 0),
        "US_WHEAT_CONT":     (0.0025, 4, 0),
        "US_RICE_CONT":      (0.01, 3, 0),
        "US_HEAT_EXCHG_CONT": (0.01, 3, 0),
    })


# ---------------------------------------------------------------------------
# Data quality thresholds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityConfig:
    """Staleness / gap / ordering thresholds used by the quality monitor."""

    max_staleness_ms: int = 250            # quote older than this -> STALE flag
    stale_warn_ms: int = 100               # below hard limit, but log a warning
    max_sequence_gap: int = 64             # gaps larger than this are "major"
    gap_recovery_window_ms: int = 5_000    # wait for resync before declaring gap
    ordering_violation_limit: int = 8      # per-symbol per-second threshold
    heartbeat_interval_ms: int = 1_000     # venue heartbeat cadence
    heartbeat_miss_limit: int = 3          # missed heartbeats -> connection suspect


# ---------------------------------------------------------------------------
# Hot-path buffering
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BufferingConfig:
    """Ring buffer sizing for the in-process hot path."""

    quote_ring_size: int = 1 << 20         # 1,048,576 quotes per symbol shard
    event_ring_size: int = 1 << 19         # 524,288 control events
    shard_count: int = 32                  # must be power of two
    drain_batch_size: int = 256            # quotes drained per consumer wakeup
    high_watermark_pct: float = 0.85        # trigger backpressure at this fill


# ---------------------------------------------------------------------------
# Aggregate service config + validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServiceConfig:
    name: str = "market-data-gateway"
    version: str = "1.0.0"
    env: str = "production"
    network: NetworkConfig = field(default_factory=NetworkConfig)
    feeds: FeedConfig = field(default_factory=FeedConfig)
    normalization: NormalizationConfig = field(default_factory=NormalizationConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    buffering: BufferingConfig = field(default_factory=BufferingConfig)


def validate_config(cfg: ServiceConfig) -> List[str]:
    """Validate the full configuration tree.

    Returns a list of human-readable error strings; an empty list means the
    configuration is valid and the service may boot.
    """
    errors: List[str] = []

    # --- network -----------------------------------------------------------
    if not (1 <= cfg.network.listen_port <= 65535):
        errors.append(f"network.listen_port out of range: {cfg.network.listen_port}")
    if cfg.network.max_connections < 1 or cfg.network.max_connections > 4096:
        errors.append(f"network.max_connections invalid: {cfg.network.max_connections}")
    if cfg.network.recv_buffer_bytes < 64 * 1024:
        errors.append("network.recv_buffer_bytes below 64 KiB minimum")
    if cfg.network.send_buffer_bytes < 64 * 1024:
        errors.append("network.send_buffer_bytes below 64 KiB minimum")
    if cfg.network.epoll_wait_timeout_ms < 0 or cfg.network.epoll_wait_timeout_ms > 500:
        errors.append("network.epoll_wait_timeout_ms outside [0, 500] window")

    # --- feeds -------------------------------------------------------------
    seen_venue_ids = set()
    for venue in cfg.feeds.venues:
        if not venue.venue_id:
            errors.append("feed venue with empty venue_id found")
            continue
        if venue.venue_id in seen_venue_ids:
            errors.append(f"duplicate venue_id in feed table: {venue.venue_id}")
        seen_venue_ids.add(venue.venue_id)
        if not (1 <= venue.port <= 65535):
            errors.append(f"feed {venue.venue_id}: port out of range {venue.port}")
        if venue.protocol not in ("FIX44", "ITCH", "MDP3"):
            errors.append(f"feed {venue.venue_id}: unsupported protocol {venue.protocol!r}")
        if venue.priority < 0:
            errors.append(f"feed {venue.venue_id}: negative priority {venue.priority}")

    # --- normalization -----------------------------------------------------
    for (venue, symbol), canonical in cfg.normalization.canonical_map.items():
        if venue not in seen_venue_ids:
            errors.append(f"canonical_map references unknown venue {venue!r} for {symbol!r}")
        if canonical not in cfg.normalization.tick_sizes:
            errors.append(f"canonical symbol {canonical!r} missing tick size entry")

    # --- quality -----------------------------------------------------------
    if cfg.quality.stale_warn_ms >= cfg.quality.max_staleness_ms:
        errors.append("quality.stale_warn_ms must be < quality.max_staleness_ms")
    if cfg.quality.max_sequence_gap < 1:
        errors.append("quality.max_sequence_gap must be >= 1")
    if cfg.quality.heartbeat_miss_limit < 1:
        errors.append("quality.heartbeat_miss_limit must be >= 1")

    # --- buffering ---------------------------------------------------------
    size = cfg.buffering.quote_ring_size
    if size <= 0 or (size & (size - 1)) != 0:
        errors.append(f"buffering.quote_ring_size must be a power of two, got {size}")
    if cfg.buffering.shard_count <= 0 or (cfg.buffering.shard_count & (cfg.buffering.shard_count - 1)) != 0:
        errors.append("buffering.shard_count must be a power of two")
    if not (0.0 < cfg.buffering.high_watermark_pct < 1.0):
        errors.append("buffering.high_watermark_pct must be in (0, 1)")

    return errors


# Singleton used by every module in the service.
CONFIG = ServiceConfig()
