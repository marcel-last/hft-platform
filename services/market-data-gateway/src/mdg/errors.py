"""market_data_gateway — error taxonomy.

Every exception raised inside the gateway is a subclass of
:class:`MDGError`.  Each concrete class carries a stable machine-readable
``code`` (used in JSON error envelopes and metrics labels) plus optional
structured context that is serialized with the exception.

The codes follow the platform-wide convention:

    MDG-<NNN>   market-data-gateway local errors
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class MDGError(Exception):
    """Base class for all market-data-gateway errors."""

    #: stable machine-readable code, e.g. "MDG-101"
    code: str = "MDG-000"
    #: HTTP-ish severity hint used by the API layer (5xx default)
    http_status: int = 500
    #: whether this error is retryable by the caller
    retryable: bool = False

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: Dict[str, Any] = context or {}

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize into the platform error envelope."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "service": "market-data-gateway",
                "retryable": self.retryable,
                "context": self.context,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, context={self.context!r})"


# ---------------------------------------------------------------------------
# Configuration errors (MDG-1xx)
# ---------------------------------------------------------------------------

class ConfigError(MDGError):
    """Raised when the service configuration fails validation at boot."""

    code = "MDG-101"
    http_status = 503
    retryable = False


class UnknownSymbolError(ConfigError):
    """A venue symbol could not be mapped to a canonical symbol."""

    code = "MDG-102"

    def __init__(self, venue_id: str, symbol_venue: str) -> None:
        super().__init__(
            f"cannot map venue symbol {symbol_venue!r} on venue {venue_id!r} to a canonical symbol",
            context={"venue_id": venue_id, "symbol_venue": symbol_venue},
        )


class MissingTickSizeError(ConfigError):
    """A canonical symbol has no tick size table entry."""

    code = "MDG-103"

    def __init__(self, canonical_symbol: str) -> None:
        super().__init__(
            f"no tick size configured for canonical symbol {canonical_symbol!r}",
            context={"canonical_symbol": canonical_symbol},
        )


# ---------------------------------------------------------------------------
# Feed / connection errors (MDG-2xx)
# ---------------------------------------------------------------------------

class FeedConnectionError(MDGError):
    """Base class for venue feed transport failures."""

    code = "MDG-200"
    http_status = 503
    retryable = True


class FeedAuthError(FeedConnectionError):
    """Venue rejected the credentials during the authentication handshake."""

    code = "MDG-201"
    retryable = False


class FeedTimeoutError(FeedConnectionError):
    """Handshake or message delivery timed out."""

    code = "MDG-202"


class FeedProtocolError(FeedConnectionError):
    """The venue stream violated the expected wire protocol."""

    code = "MDG-203"
    retryable = False

    def __init__(self, venue_id: str, reason: str, seq_no: Optional[int] = None) -> None:
        super().__init__(
            f"protocol violation on {venue_id!r}: {reason}",
            context={"venue_id": venue_id, "reason": reason, "seq_no": seq_no},
        )


class FeedSequenceGapError(MDGError):
    """A sequence gap larger than the configured threshold was observed."""

    code = "MDG-204"
    retryable = True

    def __init__(self, venue_id: str, symbol_venue: str, gap_size: int) -> None:
        super().__init__(
            f"sequence gap of {gap_size} on {venue_id!r}/{symbol_venue!r}",
            context={"venue_id": venue_id, "symbol_venue": symbol_venue, "gap_size": gap_size},
        )


# ---------------------------------------------------------------------------
# Normalization / quality errors (MDG-3xx)
# ---------------------------------------------------------------------------

class NormalizationError(MDGError):
    """Base class for normalization pipeline failures."""

    code = "MDG-300"
    http_status = 500


class TickSizeViolation(NormalizationError):
    """A quote price is not a multiple of the instrument tick size."""

    code = "MDG-301"

    def __init__(self, canonical_symbol: str, price: float, tick_size: float) -> None:
        super().__init__(
            f"price {price} for {canonical_symbol!r} is not on the {tick_size} tick grid",
            context={"canonical_symbol": canonical_symbol, "price": price, "tick_size": tick_size},
        )


class StalenessBreachError(NormalizationError):
    """A quote exceeded the hard staleness budget and was dropped."""

    code = "MDG-302"
    retryable = False


# ---------------------------------------------------------------------------
# Distribution / API errors (MDG-4xx)
# ---------------------------------------------------------------------------

class SubscriberError(MDGError):
    """Base class for consumer/subscriber management errors."""

    code = "MDG-400"
    http_status = 400


class SubscriberNotFoundError(SubscriberError):
    """The requested subscriber id does not exist on this gateway."""

    code = "MDG-401"
    http_status = 404

    def __init__(self, subscriber_id: str) -> None:
        super().__init__(f"subscriber {subscriber_id!r} not found", context={"subscriber_id": subscriber_id})


class BackpressureError(MDGError):
    """A consumer fell behind and the gateway is shedding load for it."""

    code = "MDG-402"
    http_status = 503
    retryable = True


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def error_envelope(exc: Exception) -> Dict[str, Any]:
    """Build the JSON error envelope for any exception.

    Non-MDG exceptions are wrapped in a generic ``MDG-999`` envelope so that
    the API layer always returns a structurally identical payload.
    """
    if isinstance(exc, MDGError):
        return exc.to_dict()
    return {
        "error": {
            "code": "MDG-999",
            "message": f"unexpected error: {exc}",
            "service": "market-data-gateway",
            "retryable": False,
            "context": {"exception_type": type(exc).__name__},
        }
    }
