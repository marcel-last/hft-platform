"""position_keeper — S6 of the HFT platform.

Authoritative real-time position state per (account, symbol).  Applies fills
from the execution gateway (S4), corporate actions, and manual adjustments.
Provides position snapshots for analytics (S9) and settlement (S14).
"""

from .config import CONFIG, ServiceConfig, validate_config
from .models import (
    AdjustmentRecord,
    CorporateAction,
    FillEvent,
    PositionKey,
    PositionSide,
    PositionState,
    now_ns,
)
from .position_engine import PositionEngine
from .errors import POSKError

__all__ = [
    "CONFIG",
    "ServiceConfig",
    "validate_config",
    "AdjustmentRecord",
    "CorporateAction",
    "FillEvent",
    "PositionKey",
    "PositionSide",
    "PositionState",
    "now_ns",
    "PositionEngine",
    "POSKError",
]
