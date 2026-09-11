"""Immutable configuration for the ``dashboard`` tool.

Follows CONVENTIONS §5 exactly: frozen dataclasses, ``validate_config()``
returning a list of error strings (empty = valid), and a module-level
``CONFIG`` singleton. No environment variables are read here — deploy-time
injection is the config-service's job; the dashboard only reads CLI flags
(wired up in ``main.py`` by constructing a fresh ``DashConfig``).
"""

from dataclasses import dataclass, field
from typing import List, Tuple


SERVICE_NAME = "dashboard"
SERVICE_VERSION = "1.0.0"
DEFAULT_PORT = 7760

#: Exactly one hour, in int64 nanoseconds (CONVENTIONS §2: no floats in
#: time math; the token TTL is an int64 ns value on the wire).
ONE_HOUR_NS = 3_600_000_000_000

@dataclass(frozen=True)
class ServiceEndpoint:
    """One probe target for the ``/api/health`` sweep."""

    name: str
    port: int
    package: str


#: The 15 platform services in boot/port order (7610 .. 7750, step 10).
#: ``name`` is the service directory name (CONVENTIONS §1.2 envelope rule);
#: ``package`` is the Python package (or crate) name, for display.
DEFAULT_SERVICES: Tuple[ServiceEndpoint, ...] = (
    ServiceEndpoint("market-data-gateway", 7610, "mdg"),
    ServiceEndpoint("order-book-builder", 7620, "obb"),
    ServiceEndpoint("strategy-engine", 7630, "ste"),
    ServiceEndpoint("execution-gateway", 7640, "exg"),
    ServiceEndpoint("risk-manager", 7650, "rkm"),
    ServiceEndpoint("position-keeper", 7660, "posk"),
    ServiceEndpoint("latency-monitor", 7670, "latmon"),
    ServiceEndpoint("data-quality-monitor", 7680, "dqm"),
    ServiceEndpoint("portfolio-analytics", 7690, "pfa"),
    ServiceEndpoint("alerting-service", 7700, "altsvc"),
    ServiceEndpoint("config-service", 7710, "cfgs"),
    ServiceEndpoint("auth-service", 7720, "authsvc"),
    ServiceEndpoint("audit-logger", 7730, "audl"),
    ServiceEndpoint("settlement-service", 7740, "stls"),
    ServiceEndpoint("api-gateway", 7750, "apigw"),
)


@dataclass(frozen=True)
class AuthConfig:
    """Token acquisition against S12 auth-service."""

    base_url: str = "http://127.0.0.1:7720"
    sub: str = "dashboard"
    scopes: Tuple[str, ...] = ("read", "write")
    token_ttl_ns: int = ONE_HOUR_NS
    connect_timeout_ms: int = 500
    read_timeout_ms: int = 1_500
    #: Proactive-refresh watermark: re-fetch when less than this fraction of
    #: the original TTL remains on the cached token.
    refresh_watermark_frac: float = 0.25


@dataclass(frozen=True)
class GatewayConfig:
    """Reverse-proxy target: S15 api-gateway."""

    base_url: str = "http://127.0.0.1:7750"
    connect_timeout_ms: int = 500
    read_timeout_ms: int = 3_000
    max_body_bytes: int = 1_048_576  # 1 MiB, matching the S15 server template


@dataclass(frozen=True)
class HealthConfig:
    """The ``/api/health`` 15-port sweep."""

    services: Tuple[ServiceEndpoint, ...] = field(default_factory=lambda: DEFAULT_SERVICES)
    probe_timeout_ms: int = 800
    host: str = "127.0.0.1"


@dataclass(frozen=True)
class DashConfig:
    """Root configuration for the dashboard tool."""

    name: str = SERVICE_NAME
    version: str = SERVICE_VERSION
    bind: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    read_only: bool = False
    auth: AuthConfig = field(default_factory=AuthConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    health: HealthConfig = field(default_factory=HealthConfig)


def validate_config(cfg: DashConfig) -> List[str]:
    """Return a list of configuration problems; empty list means valid."""
    errors: List[str] = []

    if cfg.name.strip() == "":
        errors.append("config.name must be non-empty")
    if cfg.bind.strip() == "":
        errors.append("config.bind must be non-empty")
    if not (1 <= cfg.port <= 65535):
        errors.append("config.port must be in [1, 65535]")

    if cfg.auth.base_url.strip() == "":
        errors.append("config.auth.base_url must be non-empty")
    if not cfg.auth.base_url.startswith(("http://", "https://")):
        errors.append("config.auth.base_url must start with http:// or https://")
    if cfg.auth.sub.strip() == "":
        errors.append("config.auth.sub must be non-empty")
    if len(cfg.auth.scopes) == 0:
        errors.append("config.auth.scopes must not be empty")
    if cfg.auth.token_ttl_ns <= 0:
        errors.append("config.auth.token_ttl_ns must be > 0")
    if cfg.auth.connect_timeout_ms <= 0 or cfg.auth.read_timeout_ms <= 0:
        errors.append("config.auth timeouts must be > 0")
    if not (0.0 < cfg.auth.refresh_watermark_frac < 1.0):
        errors.append("config.auth.refresh_watermark_frac must be in (0, 1)")

    if cfg.gateway.base_url.strip() == "":
        errors.append("config.gateway.base_url must be non-empty")
    if not cfg.gateway.base_url.startswith(("http://", "https://")):
        errors.append("config.gateway.base_url must start with http:// or https://")
    if cfg.gateway.connect_timeout_ms <= 0 or cfg.gateway.read_timeout_ms <= 0:
        errors.append("config.gateway timeouts must be > 0")
    if cfg.gateway.max_body_bytes <= 0:
        errors.append("config.gateway.max_body_bytes must be > 0")

    ports_seen = set()
    if len(cfg.health.services) == 0:
        errors.append("config.health.services must not be empty")
    for svc in cfg.health.services:
        if svc.name.strip() == "" or svc.package.strip() == "":
            errors.append("config.health: service name and package must be non-empty")
        if not (1 <= svc.port <= 65535):
            errors.append("config.health: port must be in [1, 65535]")
        if svc.port in ports_seen:
            errors.append("config.health: duplicate port %d" % svc.port)
        ports_seen.add(svc.port)
    if cfg.health.probe_timeout_ms <= 0:
        errors.append("config.health.probe_timeout_ms must be > 0")

    return errors


#: Module-level singleton (CONVENTIONS §5). ``main.py`` replaces the effective
#: config with a CLI-derived instance before boot.
CONFIG = DashConfig()
