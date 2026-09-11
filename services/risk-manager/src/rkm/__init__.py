"""risk_manager (rkm) — S5 pre-trade and real-time risk management.

Public API re-exports for convenience; import submodules directly for full access.
"""

from .config import CONFIG, ServiceConfig, validate_config
from .errors import RKMError, error_envelope
from .models import (
    BreachRecord,
    BreachSeverity,
    CheckResult,
    CheckVerdict,
    KillSwitchState,
    OrderRecord,
    PreTradeRequest,
    PositionState,
    RiskLimits,
    RiskSide,
    now_ns,
)
from .risk_engine import RiskEngine

__all__ = [
    "CONFIG",
    "ServiceConfig",
    "validate_config",
    "RKMError",
    "error_envelope",
    "BreachRecord",
    "BreachSeverity",
    "CheckResult",
    "CheckVerdict",
    "KillSwitchState",
    "OrderRecord",
    "PreTradeRequest",
    "PositionState",
    "RiskLimits",
    "RiskSide",
    "now_ns",
    "RiskEngine",
]
