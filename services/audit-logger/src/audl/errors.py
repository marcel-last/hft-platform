"""audit_logger — error taxonomy.

Codes follow the platform convention (CONVENTIONS §1.1): ``AUD-<NNN>``, grouped
by class — ``AUD-0xx`` protocol/boot, ``AUD-1xx`` configuration, ``AUD-2xx``
request/validation, ``AUD-4xx`` routing.  Every error serializes to the standard
envelope (CONVENTIONS §1.2) via :func:`error_envelope`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

SERVICE_NAME = "audit-logger"


class AUDError(Exception):
    """Base class for all audit-logger errors (CONVENTIONS §1.3)."""

    code: str = "AUD-000"
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
                "service": SERVICE_NAME,
                "retryable": self.retryable,
                "context": self.context,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# ---------------------------------------------------------------------------
# AUD-0xx — protocol / boot
# ---------------------------------------------------------------------------

class AUDProtocolError(AUDError):
    """The HTTP request could not be parsed (bad JSON body, unreadable stream)."""

    code = "AUD-001"
    http_status = 400


class AUDBootError(AUDError):
    """The service failed to boot (config invalid, port already in use)."""

    code = "AUD-002"
    http_status = 500
    retryable = True


# ---------------------------------------------------------------------------
# AUD-1xx — configuration
# ---------------------------------------------------------------------------

class AUDConfigError(AUDError):
    """``validate_config()`` rejected the boot configuration."""

    code = "AUD-101"
    http_status = 503


# ---------------------------------------------------------------------------
# AUD-2xx — request / validation
# ---------------------------------------------------------------------------

class InvalidEventError(AUDError):
    """A POST /events body is missing a required field or has a bad type.

    ``field`` carries the name of the offending field (or ``""`` when the whole
    body is malformed, e.g. not a JSON object).
    """

    code = "AUD-201"
    http_status = 400

    def __init__(self, field: str, detail: str = "") -> None:
        msg = f"invalid audit event: {detail if detail else 'missing required field: ' + field}"
        ctx: Dict[str, Any] = {"field": field}
        if detail:
            ctx["detail"] = detail
        super().__init__(msg, context=ctx)


class BadIdError(AUDError):
    """A GET /events/{id} path segment is not a well-formed event id."""

    code = "AUD-202"
    http_status = 400

    def __init__(self, raw: str) -> None:
        super().__init__(
            f"malformed event id {raw!r}: expected EVT-<12 lowercase hex digits>",
            context={"raw": raw},
        )


class BadLimitError(AUDError):
    """A ?limit= query parameter is not a positive integer."""

    code = "AUD-203"
    http_status = 400

    def __init__(self, raw: str) -> None:
        super().__init__(
            f"limit must be a positive integer, got {raw!r}",
            context={"raw": raw},
        )


class UnknownEventError(AUDError):
    """The requested event id does not exist in the chain (or was evicted)."""

    code = "AUD-204"
    http_status = 404

    def __init__(self, event_id: str, evicted: bool = False) -> None:
        msg = (
            f"no audit event with id {event_id!r}: it was evicted past the retention window"
            if evicted
            else f"no audit event with id {event_id!r}"
        )
        ctx: Dict[str, Any] = {"event_id": event_id, "evicted": evicted}
        super().__init__(msg, context=ctx)


# ---------------------------------------------------------------------------
# AUD-4xx — routing
# ---------------------------------------------------------------------------

class AUDNoRouteError(AUDError):
    """No route matches (method, path); the router default (CONVENTIONS §3)."""

    code = "AUD-404"
    http_status = 404

    def __init__(self, method: str, path: str) -> None:
        super().__init__(
            f"no route for {method} {path}",
            context={"method": method, "path": path},
        )


def error_envelope(exc: Exception) -> Dict[str, Any]:
    """Serialize any exception to the standard platform error envelope (§1.2)."""
    if isinstance(exc, AUDError):
        return exc.to_dict()
    return {
        "error": {
            "code": "AUD-999",
            "message": f"unexpected error: {exc}",
            "service": SERVICE_NAME,
            "retryable": False,
            "context": {"exception_type": type(exc).__name__},
        }
    }
