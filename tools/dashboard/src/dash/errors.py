"""Error hierarchy for the ``dashboard`` tool (CONVENTIONS §1.2 / §1.3).

The dashboard's *own* failures use ``UI-NNN`` codes in the standard envelope.
Upstream failures (auth-service / api-gateway / S9 / S14) are **never**
re-written: their status + body pass through verbatim (see ``clients.py``),
so a client always sees the originating service's ``PREFIX-NNN`` envelope
untouched.

Code classes:
    UI-0xx  internal / unexpected
    UI-2xx  request problems understood by the dashboard itself
    UI-4xx  upstream/transport failures (5xx HTTP, retryable)
    UI-404 / UI-405  routing (mirror the HTTP status, not retryable)
"""

from typing import Any, Dict, Optional


class DashError(Exception):
    """Base exception for the dashboard tool."""

    code: str = "UI-001"
    message: str = "Unexpected internal error in the dashboard."
    status: int = 500
    retryable: bool = False

    def __init__(self, message: Optional[str] = None,
                 context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.context: Dict[str, Any] = dict(context or {})


class InternalError(DashError):
    """UI-001 — an unhandled internal failure reached the error path."""

    code = "UI-001"
    status = 500
    retryable = False


class MalformedBodyError(DashError):
    """UI-201 — the request body is missing where required, or is not JSON."""

    code = "UI-201"
    status = 400
    retryable = False


class UnsupportedParamError(DashError):
    """UI-202 — a dashboard-local query parameter carried an unsupported value."""

    code = "UI-202"
    status = 400
    retryable = False


class ReadOnlyViolationError(DashError):
    """UI-205 — a write-scope route was called while ``--read-only`` is set."""

    code = "UI-205"
    status = 403
    retryable = False


class AuthUnreachableError(DashError):
    """UI-401 — the auth-service could not be reached for token acquisition."""

    code = "UI-401"
    status = 502
    retryable = True


class GatewayTransportError(DashError):
    """UI-402 — the api-gateway could not be reached (connect/read timeout)."""

    code = "UI-402"
    status = 502
    retryable = True


class TokenRefreshFailedError(DashError):
    """UI-403 — the token re-fetch failed after the gateway rejected a token."""

    code = "UI-403"
    status = 502
    retryable = True


class UnknownRouteError(DashError):
    """UI-404 — the path is not served by the dashboard at all."""

    code = "UI-404"
    status = 404
    retryable = False


class MethodNotAllowedError(DashError):
    """UI-405 — the path is known but not for this HTTP method."""

    code = "UI-405"
    status = 405
    retryable = False


def error_envelope(exc: Exception) -> Dict[str, Any]:
    """Serialize any exception to the CONVENTIONS §1.2 error envelope.

    ``DashError`` subclasses carry their own code/message/status/retryable;
    anything else falls back to :class:`InternalError` so the wire shape is
    always exactly the documented envelope.
    """
    if isinstance(exc, DashError):
        code = exc.code
        message = exc.message
        service = "dashboard"
        retryable = exc.retryable
        context: Dict[str, Any] = dict(getattr(exc, "context", None) or {})
    else:  # pragma: no cover - defensive; main's dispatch catches DashError
        code = InternalError.code
        message = "Unexpected internal error: %s" % type(exc).__name__
        service = "dashboard"
        retryable = False
        context = {"exception": type(exc).__name__}
    return {
        "error": {
            "code": code,
            "message": message,
            "service": service,
            "retryable": retryable,
            "context": context,
        }
    }


def error_response(exc: Exception) -> "tuple":  # (status, body)
    """Convenience: ``(http_status, envelope_body)`` for a ``DashError``."""
    status = exc.status if isinstance(exc, DashError) else 500
    return status, error_envelope(exc)
