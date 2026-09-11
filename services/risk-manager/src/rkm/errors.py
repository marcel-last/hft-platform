"""risk_manager — error taxonomy.

Codes follow the platform convention: ``RKM-<NNN>`` for risk-manager.  Every
error serializes to the standard envelope::

    {"error": {code, message, service, retryable, context}}
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class RKMError(Exception):
    """Base class for all risk-manager errors."""

    code: str = "RKM-000"
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
                "service": "risk-manager",
                "retryable": self.retryable,
                "context": self.context,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class RKMConfigError(RKMError):
    """Configuration failed validation at boot."""

    code = "RKM-101"
    http_status = 503


class InvalidPreTradeRequestError(RKMError):
    """A pre-trade request body was malformed or missing required fields."""

    code = "RKM-201"
    http_status = 400
    retryable = False


class UnknownSymbolError(RKMError):
    """A request referenced a symbol the risk manager does not track yet."""

    code = "RKM-202"
    http_status = 404
    retryable = False

    def __init__(self, canonical_symbol: str) -> None:
        super().__init__(
            f"no position state maintained for {canonical_symbol!r}",
            context={"canonical_symbol": canonical_symbol},
        )


class KillSwitchAlreadyEngagedError(RKMError):
    """An engage request was issued while the kill-switch is already engaged."""

    code = "RKM-203"
    http_status = 409
    retryable = False

    def __init__(self) -> None:
        super().__init__("kill-switch is already engaged", context={})


class KillSwitchNotEngagedError(RKMError):
    """A disengage request was issued while the kill-switch is disarmed."""

    code = "RKM-204"
    http_status = 409
    retryable = False

    def __init__(self) -> None:
        super().__init__("kill-switch is not engaged", context={})


class UpstreamUnreachableError(RKMError):
    """An upstream service (S2 book builder or S4 execution gateway) could not be reached."""

    code = "RKM-301"
    http_status = 503
    retryable = True

    def __init__(self, target: str, detail: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        ctx = dict(context or {})
        ctx.setdefault("target", target)
        super().__init__(f"cannot reach {target}: {detail}", context=ctx)


class ExecutionGatewayError(RKMError):
    """The execution gateway (S4) rejected a control request (e.g. flatten)."""

    code = "RKM-302"
    http_status = 502
    retryable = True


class EngineNotReadyError(RKMError):
    """The risk engine has not been initialized (request before boot completed)."""

    code = "RKM-500"
    http_status = 503
    retryable = True


def error_envelope(exc: Exception) -> Dict[str, Any]:
    """Serialize any exception to the standard platform error envelope."""
    if isinstance(exc, RKMError):
        return exc.to_dict()
    return {
        "error": {
            "code": "RKM-999",
            "message": f"unexpected error: {exc}",
            "service": "risk-manager",
            "retryable": False,
            "context": {"exception_type": type(exc).__name__},
        }
    }
