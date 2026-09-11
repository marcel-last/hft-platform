"""market_data_gateway package.

Public surface:
    CONFIG, ServiceConfig, validate_config
    QuoteNormalizer, QualityMonitor, ShardedRingBuffer, RingBuffer
    FeedClient, SocketTransport, MockVenueTransport
    GatewayController, build_router
    main
"""

from .config import CONFIG, NetworkConfig, FeedConfig, NormalizationConfig, QualityConfig, BufferingConfig, ServiceConfig, VenueFeed, validate_config
from .models import (
    BookLevel, ControlEvent, ControlEventKind, DataQuality, OrderBookSnapshot,
    Quote, QuoteAction, QualityMetrics, RawHeartbeat, RawQuote, RawTrade,
    SequenceTracker, Side, TradePrint, VenueStatus,
    book_snapshot_to_wire, control_event_to_wire, now_ns, quote_to_wire, trade_to_wire,
)
from .errors import (
    BackpressureError, ConfigError, FeedAuthError, FeedConnectionError,
    FeedProtocolError, FeedSequenceGapError, FeedTimeoutError, MDGError,
    MissingTickSizeError, NormalizationError, StalenessBreachError,
    SubscriberError, SubscriberNotFoundError, TickSizeViolation, UnknownSymbolError,
    error_envelope,
)
from .normalizer import QuoteNormalizer
from .quality_monitor import QualityMonitor
from .ring_buffer import RingBuffer, ShardedRingBuffer
from .feed_client import FeedClient, SocketTransport, Transport, frame_decode, frame_encode
from .mock_feed import MockVenueTransport
from .controller import GatewayController
from .router import Router, Route, build_router

__version__ = "1.0.0"

__all__ = [
    "CONFIG", "NetworkConfig", "FeedConfig", "NormalizationConfig", "QualityConfig",
    "BufferingConfig", "ServiceConfig", "VenueFeed", "validate_config",
    "BookLevel", "ControlEvent", "ControlEventKind", "DataQuality",
    "OrderBookSnapshot", "Quote", "QuoteAction", "QualityMetrics", "RawHeartbeat",
    "RawQuote", "RawTrade", "SequenceTracker", "Side", "TradePrint", "VenueStatus",
    "book_snapshot_to_wire", "control_event_to_wire", "now_ns", "quote_to_wire",
    "trade_to_wire",
    "BackpressureError", "ConfigError", "FeedAuthError", "FeedConnectionError",
    "FeedProtocolError", "FeedSequenceGapError", "FeedTimeoutError", "MDGError",
    "MissingTickSizeError", "NormalizationError", "StalenessBreachError",
    "SubscriberError", "SubscriberNotFoundError", "TickSizeViolation",
    "UnknownSymbolError", "error_envelope",
    "QuoteNormalizer", "QualityMonitor", "RingBuffer", "ShardedRingBuffer",
    "FeedClient", "SocketTransport", "Transport", "frame_decode", "frame_encode",
    "MockVenueTransport", "GatewayController", "Router", "Route", "build_router",
]
