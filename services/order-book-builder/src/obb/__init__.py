"""order_book_builder package."""

from .config import BookConfig, IngestConfig, SnapshotConfig, ServiceConfig, CONFIG, validate_config
from .models import (
    BookEvent, BookEventKind, BookHealth, BookSide, OrderBook, PriceLevel, TopOfBook, now_ns,
)
from .errors import (
    OBBConfigError, OBBError, BookCrossAnomaly, BookCorruptionError,
    BookNotReadyError, GatewayUnreachableError, UnknownBookSymbolError, error_envelope,
)
from .book_engine import BookEngine
from .gateway_client import GatewayClient
from .controller import BookBuilderController
from .router import Router, Route, build_router

__version__ = "1.0.0"

__all__ = [
    "BookConfig", "IngestConfig", "SnapshotConfig", "ServiceConfig", "CONFIG", "validate_config",
    "BookEvent", "BookEventKind", "BookHealth", "BookSide", "OrderBook", "PriceLevel",
    "TopOfBook", "now_ns",
    "OBBConfigError", "OBBError", "BookCrossAnomaly", "BookCorruptionError",
    "BookNotReadyError", "GatewayUnreachableError", "UnknownBookSymbolError", "error_envelope",
    "BookEngine", "GatewayClient", "BookBuilderController", "Router", "Route", "build_router",
]
