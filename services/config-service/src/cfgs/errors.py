"""config_service — error taxonomy.

Codes follow the platform convention: ``CFG-<NNN>`` for config-service.
Every error serializes to the standard envelope::

    {"error": {code, message, service, retryable, context}}
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class CFGError(Exception):
    """Base class for all config-service errors."""

    code: str = "CFG-000"
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
                "service": "config-service",
                "retryable": self.retryable,
                "context": self.context,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class CFGConfigError(CFGError):
    """Configuration failed validation at boot."""

    code = "CFG-101"
    http_status = 503


class InvalidPayloadError(CFGError):
    """A submitted configuration payload is not a JSON object."""

    code = "CFG-201"
    http_status = 400

    def __init__(self, service: str) -> None:
        super().__init__(
            f"configuration payload for {service!r} must be a JSON object",
            context={"service": service},
        )


class UnknownServiceError(CFGError):
    """A request referenced a service with no stored configuration."""

    code = "CFG-203"
    http_status = 404

    def __init__(self, service: str) -> None:
        super().__init__(
            f"no configuration stored for service={service!r}",
            context={"service": service},
        )


class InvalidFlagError(CFGError):
    """A feature-flag operation referenced an unknown flag or bad value."""

    code = "CFG-204"
    http_status = 400


class StoreFullError(CFGError):
    """A safety cap on the number of tracked services was reached."""

    code = "CFG-205"
    http_status = 409

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"service store is full (limit {limit}); refusing to track a new service",
            context={"limit": limit},
        )


class UpstreamUnreachableError(CFGError):
    """A best-effort fan-out (alerting / audit) could not be delivered."""

    code = "CFG-301"
    http_status = 503
    retryable = True

    def __init__(self, target: str, detail: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        ctx = dict(context or {})
        ctx.setdefault("target", target)
        super().__init__(f"cannot reach {target}: {detail}", context=ctx)


class StoreNotReadyError(CFGError):
    """The config store has not been initialized (request before boot completed)."""

    code = "CFG-500"
    http_status = 503
    retryable = True


def error_envelope(exc: Exception) -> Dict[str, Any]:
    """Serialize any exception to the standard platform error envelope."""
    if isinstance(exc, CFGError):
        return exc.to_dict()
    return {
        "error": {
            "code": "CFG-999",
            "message": f"unexpected error: {exc}",
            "service": "config-service",
            "retryable": False,
            "context": {"exception_type": type(exc).__name__},
        }
    }
