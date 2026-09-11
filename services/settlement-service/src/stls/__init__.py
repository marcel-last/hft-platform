"""stls — HFT platform S14: settlement-service.

End-of-day and intraday settlement reconciliation for one trading desk:
idempotent fill settlement per settlement date, venue-statement matching
(quantity/price tolerances), net-position and cash roll-ups, discrepancy
flagging, and tamper-evident EOD sealing (SHA-256 over canonical JSON).

Public API (see the individual modules for full signatures):

* :class:`stls.config.SettlementConfig` / :func:`stls.config.validate_config`
* :class:`stls.settlement_engine.SettlementEngine`
* :class:`stls.controller.SettlementController`
* :func:`stls.router.build_router`
* :mod:`stls.main` — ``python -m stls.main``
"""

from .config import SettlementConfig, validate_config, CONFIG
from .models import (
    Clock,
    ManualClock,
    SystemClock,
    FillRecord,
    StatementLine,
    Discrepancy,
    SettlementRun,
    canonical_json,
    content_hash,
    now_ns,
)
from .errors import STLError, error_envelope
from .settlement_engine import SettlementEngine
from .controller import SettlementController
from .router import build_router
from .reconcile import matching_report
from .netting import compute_net_positions, compute_cash_summary

__version__ = "1.0.0"

__all__ = [
    "SettlementConfig", "validate_config", "CONFIG",
    "Clock", "ManualClock", "SystemClock",
    "FillRecord", "StatementLine", "Discrepancy", "SettlementRun",
    "canonical_json", "content_hash", "now_ns",
    "STLError", "error_envelope",
    "SettlementEngine", "SettlementController", "build_router",
    "matching_report", "compute_net_positions", "compute_cash_summary",
    "__version__",
]
