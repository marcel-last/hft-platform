"""data_quality_monitor — S8 of the HFT platform.

Unified data-quality observability plane.  Aggregates per-symbol feed metrics
from the market-data gateway (S1) and book health from the order-book-builder
(S2), computes a composite quality score with hysteresis, and fires degradation
alerts to the alerting service (S10).
"""

from .config import CONFIG, ServiceConfig, validate_config
from .models import (
    BookHealthRow,
    DegradationEvent,
    DegradationKind,
    FeedMetrics,
    GapRecord,
    QualityState,
    Severity,
    StalenessSample,
    SymbolQuality,
    now_ns,
)
from .quality_engine import QualityEngine, severity_for_score
from .errors import DQMErrors

__all__ = [
    "CONFIG",
    "ServiceConfig",
    "validate_config",
    "BookHealthRow",
    "DegradationEvent",
    "DegradationKind",
    "FeedMetrics",
    "GapRecord",
    "QualityState",
    "Severity",
    "StalenessSample",
    "SymbolQuality",
    "now_ns",
    "QualityEngine",
    "severity_for_score",
    "DQMErrors",
]
