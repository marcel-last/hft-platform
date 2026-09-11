"""portfolio_analytics — S9 of the HFT platform.

Risk & performance plane for the platform's book of positions.  Pulls the
authoritative position book from the position-keeper (S6) and computes realized
+ unrealized P&L, Sharpe ratio, drawdown, VaR/CVaR, and per-symbol attribution.
Serves historical and real-time portfolio metrics to the api-gateway (S15).
"""

from .config import CONFIG, ServiceConfig, validate_config
from .models import (
    AttributionRow,
    EquitySample,
    PnlRow,
    PortfolioMetrics,
    PositionRow,
    VarResult,
    now_ns,
)
from .analytics_engine import AnalyticsEngine
from .errors import PFAError

__all__ = [
    "CONFIG",
    "ServiceConfig",
    "validate_config",
    "AttributionRow",
    "EquitySample",
    "PnlRow",
    "PortfolioMetrics",
    "PositionRow",
    "VarResult",
    "now_ns",
    "AnalyticsEngine",
    "PFAError",
]
