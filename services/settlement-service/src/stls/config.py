"""Configuration for the settlement service (S14).

Every namespace is a frozen dataclass; ``validate_config`` returns a list of
human-readable error strings (empty list = valid) and is invoked exactly once
at boot in :mod:`stls.main`.  No environment-variable reading happens here —
runtime overrides are the job of the config service at deploy time.

Namespaces
----------
* **ServerConfig**      -- HTTP bind defaults.
* **MatchingConfig**    -- match tolerances for fill-vs-statement reconciliation.
* **NettingConfig**     -- how net positions / net cash are computed and bounded.
* **RetentionConfig**   -- bounds on retained settlement runs, lines and stats.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server defaults (main.py may override bind/port from CLI flags)."""

    host: str = "0.0.0.0"
    port: int = 7740
    upstream_pull_timeout_ms: int = 500   # connect timeout for S6 position pulls
    upstream_read_timeout_s: float = 2.0  # socket read timeout for a single pull


@dataclass(frozen=True)
class MatchingConfig:
    """Tolerances applied when matching internal fills against venue statements."""

    price_tolerance_abs: float = 1e-6      # absolute price tolerance (currency units)
    qty_tolerance: int = 0                 # allowed |our_qty - stmt_qty| before a QTY_MISMATCH
    max_statement_lines_per_venue: int = 4096


@dataclass(frozen=True)
class NettingConfig:
    """Net position and net cash behaviour for settlement reports."""

    max_net_positions_per_run: int = 256
    zero_out_flat_positions: bool = True   # omit rows where net qty and net notional are both ~0


@dataclass(frozen=True)
class RetentionConfig:
    """Bounded retention of settlement runs, statement lines and discrepancy history."""

    max_runs_per_date_history: int = 8      # superseded EOD revisions kept per date
    max_discrepancy_lines: int = 512        # bound on open discrepancies surfaced by GET /discrepancies
    max_report_dates: int = 365             # bound on generated report dates in stats


@dataclass(frozen=True)
class IngestConfig:
    """Background ingest of S6 position snapshots into today's settlement run."""

    enabled: bool = True
    position_keeper_url: str = "http://127.0.0.1:7660"  # S6 default
    interval_ms: int = 15000        # pull cadence
    fail_log_limit: int = 200       # cap on consecutive-failure log noise


@dataclass(frozen=True)
class SettlementConfig:
    """Root configuration object for the settlement service."""

    name: str = "settlement-service"
    version: str = "1.0.0"
    env: str = "production"
    server: ServerConfig = field(default_factory=ServerConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    netting: NettingConfig = field(default_factory=NettingConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)


def validate_config(cfg: SettlementConfig) -> List[str]:
    """Return a list of configuration error strings; an empty list means valid."""

    errors: List[str] = []

    if cfg.name != "settlement-service":
        errors.append(f"name must be 'settlement-service', got {cfg.name!r}")
    if not cfg.version or any(ch in cfg.version for ch in "\n\r"):
        errors.append("version must be a non-empty single-line string")

    srv = cfg.server
    if not (1 <= srv.port <= 65535):
        errors.append(f"server.port out of range: {srv.port}")
    if not srv.host:
        errors.append("server.host must be non-empty")
    if srv.upstream_pull_timeout_ms < 1:
        errors.append("server.upstream_pull_timeout_ms must be >= 1")
    if srv.upstream_read_timeout_s < 0.1:
        errors.append("server.upstream_read_timeout_s must be >= 0.1")

    m = cfg.matching
    if m.price_tolerance_abs < 0:
        errors.append(f"matching.price_tolerance_abs must be >= 0, got {m.price_tolerance_abs}")
    if not (0 <= m.qty_tolerance <= 1_000):
        errors.append(f"matching.qty_tolerance out of range [0, 1000]: {m.qty_tolerance}")
    if m.max_statement_lines_per_venue < 1:
        errors.append("matching.max_statement_lines_per_venue must be >= 1")

    n = cfg.netting
    if n.max_net_positions_per_run < 1:
        errors.append("netting.max_net_positions_per_run must be >= 1")

    r = cfg.retention
    if r.max_runs_per_date_history < 1:
        errors.append("retention.max_runs_per_date_history must be >= 1")
    if r.max_discrepancy_lines < 1:
        errors.append("retention.max_discrepancy_lines must be >= 1")
    if r.max_report_dates < 1:
        errors.append("retention.max_report_dates must be >= 1")

    ing = cfg.ingest
    if not ing.position_keeper_url.startswith(("http://", "https://")):
        errors.append(f"ingest.position_keeper_url must be an http(s) URL, got {ing.position_keeper_url!r}")
    if ing.interval_ms < 100:
        errors.append(f"ingest.interval_ms must be >= 100, got {ing.interval_ms}")
    if ing.fail_log_limit < 1:
        errors.append(f"ingest.fail_log_limit must be >= 1, got {ing.fail_log_limit}")

    return errors


#: Module-level singleton used by every other module in the package.
CONFIG = SettlementConfig()
