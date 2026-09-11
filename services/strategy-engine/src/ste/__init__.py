"""strategy_engine package."""

from .config import (
    EmitConfig, IngestConfig, MeanReversionParams, MomentumParams, ServiceConfig,
    SignalConfig, SpreadArbParams, StrategyConfig, CONFIG, validate_config,
)
from .models import (
    BookView, MidSample, OrderIntent, RollingWindow, Signal, SignalSide,
    SignalStatus, StrategyState, now_ns,
)
from .errors import (
    ExecutionGatewayError, EngineNotReadyError, RiskLimitExceededError, STEConfigError,
    STEError, StaleBookError, UpstreamUnreachableError, UnknownStrategyError,
    error_envelope,
)
from .strategies import (
    MeanReversionStrategy, MomentumStrategy, SignalDecision, SpreadArbStrategy,
    Strategy, StrategyContext,
)
from .signal_engine import SignalEngine
from .clients import BookBuilderClient, ExecutionClient, GatewayClient
from .controller import StrategyController
from .router import Router, Route, build_router

__version__ = "1.0.0"

__all__ = [
    # config
    "EmitConfig", "IngestConfig", "MeanReversionParams", "MomentumParams",
    "ServiceConfig", "SignalConfig", "SpreadArbParams", "StrategyConfig",
    "CONFIG", "validate_config",
    # models
    "BookView", "MidSample", "OrderIntent", "RollingWindow", "Signal",
    "SignalSide", "SignalStatus", "StrategyState", "now_ns",
    # errors
    "ExecutionGatewayError", "EngineNotReadyError", "RiskLimitExceededError",
    "STEConfigError", "STEError", "StaleBookError", "UpstreamUnreachableError",
    "UnknownStrategyError", "error_envelope",
    # strategies
    "MeanReversionStrategy", "MomentumStrategy", "SignalDecision",
    "SpreadArbStrategy", "Strategy", "StrategyContext",
    # engine / clients / http
    "SignalEngine", "BookBuilderClient", "ExecutionClient", "GatewayClient",
    "StrategyController", "Router", "Route", "build_router",
]
