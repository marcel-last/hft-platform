"""strategy_engine — error taxonomy.

Codes follow the platform convention: ``STE-<NNN>`` for strategy-engine.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class STEError(Exception):
    """Base class for all strategy-engine errors."""

    code: str = "STE-000"
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
                "service": "strategy-engine",
                "retryable": self.retryable,
                "context": self.context,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class STEConfigError(STEError):
    """Configuration failed validation at boot."""

    code = "STE-101"
    http_status = 503


class UnknownStrategyError(STEError):
    """A request referenced a strategy id that is not registered."""

    code = "STE-201"
    http_status = 404
    retryable = False

    def __init__(self, strategy_id: str) -> None:
        super().__init__(
            f"no strategy registered with id {strategy_id!r}",
            context={"strategy_id": strategy_id},
        )


class UnknownSymbolError(STEError):
    """A signal/order request referenced a symbol the engine does not track."""

    code = "STE-202"
    http_status = 404
    retryable = False

    def __init__(self, canonical_symbol: str) -> None:
        super().__init__(
            f"no signal state maintained for {canonical_symbol!r}",
            context={"canonical_symbol": canonical_symbol},
        )


class StaleBookError(STEError):
    """A strategy requested to run against a book that is stale or empty."""

    code = "STE-203"
    http_status = 503
    retryable = True


class UpstreamUnreachableError(STEError):
    """An upstream service (S1 gateway or S2 book builder) could not be reached."""

    code = "STE-301"
    http_status = 503
    retryable = True

    def __init__(self, target: str, detail: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        ctx = dict(context or {})
        ctx.setdefault("target", target)
        super().__init__(f"cannot reach {target}: {detail}", context=ctx)


class ExecutionGatewayError(STEError):
    """The execution gateway (S4) rejected or could not receive an order intent."""

    code = "STE-302"
    http_status = 502
    retryable = True


class RiskLimitExceededError(STEError):
    """An emitted order intent would violate a configured risk cap."""

    code = "STE-401"
    http_status = 409
    retryable = False


class EngineNotReadyError(STEError):
    """The engine has not been initialized (e.g. request before boot completed)."""

    code = "STE-500"
    http_status = 503
    retryable = True


def error_envelope(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, STEError):
        return exc.to_dict()
    return {
        "error": {
            "code": "STE-999",
            "message": f"unexpected error: {exc}",
            "service": "strategy-engine",
            "retryable": False,
            "context": {"exception_type": type(exc).__name__},
        }
    }
