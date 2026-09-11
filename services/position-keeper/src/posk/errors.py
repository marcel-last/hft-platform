"""position_keeper — error taxonomy.

Codes follow the platform convention: ``POS-<NNN>`` for position-keeper.  Every
error serializes to the standard envelope::

    {"error": {code, message, service, retryable, context}}
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class POSKError(Exception):
    """Base class for all position-keeper errors."""

    code: str = "POS-000"
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
                "service": "position-keeper",
                "retryable": self.retryable,
                "context": self.context,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class POSKConfigError(POSKError):
    """Configuration failed validation at boot."""

    code = "POS-101"
    http_status = 503


class InvalidAdjustmentError(POSKError):
    """A manual adjustment request was malformed or missing required fields."""

    code = "POS-201"
    http_status = 400
    retryable = False


class InvalidCorporateActionError(POSKError):
    """A corporate-action request was malformed (bad type, bad factor, ...)."""

    code = "POS-202"
    http_status = 400
    retryable = False


class UnknownPositionError(POSKError):
    """A request referenced a (account, symbol) position that does not exist."""

    code = "POS-203"
    http_status = 404
    retryable = False

    def __init__(self, account: str, symbol: str) -> None:
        super().__init__(
            f"no position maintained for account={account!r} symbol={symbol!r}",
            context={"account": account, "symbol": symbol},
        )


class AdjustmentConflictError(POSKError):
    """An adjustment would drive the position to an invalid state (e.g. negative qty)."""

    code = "POS-204"
    http_status = 409
    retryable = False


class DuplicateFillError(POSKError):
    """A fill id was already applied; re-application is rejected."""

    code = "POS-205"
    http_status = 409
    retryable = False

    def __init__(self, fill_id: str) -> None:
        super().__init__(
            f"fill {fill_id!r} was already applied to the position ledger",
            context={"fill_id": fill_id},
        )


class UpstreamUnreachableError(POSKError):
    """An upstream service (S4 execution gateway or S2 book builder) could not be reached."""

    code = "POS-301"
    http_status = 503
    retryable = True

    def __init__(self, target: str, detail: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        ctx = dict(context or {})
        ctx.setdefault("target", target)
        super().__init__(f"cannot reach {target}: {detail}", context=ctx)


class EngineNotReadyError(POSKError):
    """The position engine has not been initialized (request before boot completed)."""

    code = "POS-500"
    http_status = 503
    retryable = True


def error_envelope(exc: Exception) -> Dict[str, Any]:
    """Serialize any exception to the standard platform error envelope."""
    if isinstance(exc, POSKError):
        return exc.to_dict()
    return {
        "error": {
            "code": "POS-999",
            "message": f"unexpected error: {exc}",
            "service": "position-keeper",
            "retryable": False,
            "context": {"exception_type": type(exc).__name__},
        }
    }
