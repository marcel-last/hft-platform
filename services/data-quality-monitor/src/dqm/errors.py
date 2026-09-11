"""data_quality_monitor — error taxonomy.

Codes follow the platform convention: ``DQM-<NNN>`` for data-quality-monitor.
Every error serializes to the standard envelope::

    {"error": {code, message, service, retryable, context}}
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class DQMErrors(Exception):
    """Base class for all data-quality-monitor errors."""

    code: str = "DQM-000"
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
                "service": "data-quality-monitor",
                "retryable": self.retryable,
                "context": self.context,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class DQMConfigError(DQMErrors):
    """Configuration failed validation at boot."""

    code = "DQM-101"
    http_status = 503


class UnknownSymbolError(DQMErrors):
    """A request referenced a symbol the monitor has no quality state for."""

    code = "DQM-203"
    http_status = 404
    retryable = False

    def __init__(self, symbol: str) -> None:
        super().__init__(
            f"no quality state maintained for symbol={symbol!r}",
            context={"symbol": symbol},
        )


class UpstreamUnreachableError(DQMErrors):
    """An upstream service (S1 gateway or S2 book builder) could not be reached."""

    code = "DQM-301"
    http_status = 503
    retryable = True

    def __init__(self, target: str, detail: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        ctx = dict(context or {})
        ctx.setdefault("target", target)
        super().__init__(f"cannot reach {target}: {detail}", context=ctx)


class EngineNotReadyError(DQMErrors):
    """The quality engine has not been initialized (request before boot completed)."""

    code = "DQM-500"
    http_status = 503
    retryable = True


def error_envelope(exc: Exception) -> Dict[str, Any]:
    """Serialize any exception to the standard platform error envelope."""
    if isinstance(exc, DQMErrors):
        return exc.to_dict()
    return {
        "error": {
            "code": "DQM-999",
            "message": f"unexpected error: {exc}",
            "service": "data-quality-monitor",
            "retryable": False,
            "context": {"exception_type": type(exc).__name__},
        }
    }
