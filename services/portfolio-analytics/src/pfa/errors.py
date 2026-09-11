"""portfolio_analytics — error taxonomy.

Codes follow the platform convention: ``PFA-<NNN>`` for portfolio-analytics.
Every error serializes to the standard envelope::

    {"error": {code, message, service, retryable, context}}
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class PFAError(Exception):
    """Base class for all portfolio-analytics errors."""

    code: str = "PFA-000"
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
                "service": "portfolio-analytics",
                "retryable": self.retryable,
                "context": self.context,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class PFAConfigError(PFAError):
    """Configuration failed validation at boot."""

    code = "PFA-101"
    http_status = 503


class UnknownSymbolError(PFAError):
    """A request referenced a symbol the analytics service has no position for."""

    code = "PFA-203"
    http_status = 404
    retryable = False

    def __init__(self, symbol: str) -> None:
        super().__init__(
            f"no position data maintained for symbol={symbol!r}",
            context={"symbol": symbol},
        )


class UpstreamUnreachableError(PFAError):
    """The upstream position-keeper (S6) could not be reached."""

    code = "PFA-301"
    http_status = 503
    retryable = True

    def __init__(self, target: str, detail: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        ctx = dict(context or {})
        ctx.setdefault("target", target)
        super().__init__(f"cannot reach {target}: {detail}", context=ctx)


class InsufficientDataError(PFAError):
    """A metric was requested but the return/equity series is too short to compute it."""

    code = "PFA-402"
    http_status = 409
    retryable = True

    def __init__(self, metric: str, have: int, need: int) -> None:
        super().__init__(
            f"insufficient data to compute {metric}: have {have} samples, need {need}",
            context={"metric": metric, "have": have, "need": need},
        )


class EngineNotReadyError(PFAError):
    """The analytics engine has not been initialized (request before boot completed)."""

    code = "PFA-500"
    http_status = 503
    retryable = True


def error_envelope(exc: Exception) -> Dict[str, Any]:
    """Serialize any exception to the standard platform error envelope."""
    if isinstance(exc, PFAError):
        return exc.to_dict()
    return {
        "error": {
            "code": "PFA-999",
            "message": f"unexpected error: {exc}",
            "service": "portfolio-analytics",
            "retryable": False,
            "context": {"exception_type": type(exc).__name__},
        }
    }
