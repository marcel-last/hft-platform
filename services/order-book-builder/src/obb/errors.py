"""order_book_builder — error taxonomy.

Codes follow the platform convention: ``OBB-<NNN>`` for order-book-builder.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class OBBError(Exception):
    """Base class for all order-book-builder errors."""

    code: str = "OBB-000"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: Dict[str, Any] = context or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "service": "order-book-builder",
                "retryable": self.retryable,
                "context": self.context,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class OBBConfigError(OBBError):
    """Configuration failed validation at boot."""

    code = "OBB-101"
    http_status = 503


class UnknownBookSymbolError(OBBError):
    """A quote arrived for a (symbol, venue) pair with no configured book."""

    code = "OBB-201"
    retryable = False

    def __init__(self, canonical_symbol: str, venue_id: str) -> None:
        super().__init__(
            f"no book maintained for {canonical_symbol!r} on {venue_id!r}",
            context={"canonical_symbol": canonical_symbol, "venue_id": venue_id},
        )


class BookNotReadyError(OBBError):
    """A snapshot was requested before the first rebuild completed."""

    code = "OBB-202"
    http_status = 503
    retryable = True


class BookCrossAnomaly(OBBError):
    """best_bid >= best_ask — indicates upstream data corruption."""

    code = "OBB-203"
    retryable = False

    def __init__(self, canonical_symbol: str, venue_id: str, bid: float, ask: float) -> None:
        super().__init__(
            f"crossed book on {canonical_symbol!r}/{venue_id!r}: bid={bid} >= ask={ask}",
            context={"canonical_symbol": canonical_symbol, "venue_id": venue_id,
                     "best_bid": bid, "best_ask": ask},
        )


class GatewayUnreachableError(OBBError):
    """The market-data-gateway could not be reached for subscription/snapshots."""

    code = "OBB-301"
    http_status = 503
    retryable = True


class BookCorruptionError(OBBError):
    """An invariant was violated inside the book engine (should never happen)."""

    code = "OBB-900"
    http_status = 500
    retryable = False


def error_envelope(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, OBBError):
        return exc.to_dict()
    return {
        "error": {
            "code": "OBB-999",
            "message": f"unexpected error: {exc}",
            "service": "order-book-builder",
            "retryable": False,
            "context": {"exception_type": type(exc).__name__},
        }
    }
