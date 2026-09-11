"""``dash`` — the HFT platform operations dashboard (tool, not a service).

M1 ships the backend API proxy only: ``/api/portfolio/*``,
``/api/settlement/*``, ``/api/health`` and ``/api/gateway-stats``, plus local
``/healthz`` and ``/readyz``. See ``tools/dashboard/PLAN.md`` for the full
M1/M2/M3 specification.

The dashboard is a *tool*, not service #16: it never appears in the
15-service table in ``STATE.md`` or in ``dependency-map.json``.
"""

from dash.config import (
    AuthConfig,
    DashConfig,
    GatewayConfig,
    HealthConfig,
    ServiceEndpoint,
    validate_config,
)
from dash.errors import (
    DashError,
    MalformedBodyError,
    ReadOnlyViolationError,
    UnsupportedParamError,
    UnknownRouteError,
    MethodNotAllowedError,
    AuthUnreachableError,
    GatewayTransportError,
    TokenRefreshFailedError,
    error_envelope,
)

__version__ = "1.0.0"

__all__ = [
    "AuthConfig",
    "DashConfig",
    "GatewayConfig",
    "HealthConfig",
    "ServiceEndpoint",
    "validate_config",
    "DashError",
    "MalformedBodyError",
    "ReadOnlyViolationError",
    "UnsupportedParamError",
    "UnknownRouteError",
    "MethodNotAllowedError",
    "AuthUnreachableError",
    "GatewayTransportError",
    "TokenRefreshFailedError",
    "error_envelope",
    "__version__",
]
