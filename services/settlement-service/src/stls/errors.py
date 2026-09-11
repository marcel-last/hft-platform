"""Exception hierarchy and standard error envelope for the settlement service.

Error code map (CONVENTIONS §1.1 / §1.2) — prefix ``STL-``:

============  ==============================================  ======  =========
code          meaning                                         status  retryable
============  ==============================================  ======  =========
STL-001       malformed JSON body on the request              400     false
STL-002       upstream position pull from S6 failed           503     true
STL-101       configuration error (boot-time only)            500     false
STL-201       invalid /settle request body field              400     false
STL-202       malformed or out-of-range settlement date param 400     false
STL-203       malformed ``limit`` query parameter             400     false
STL-204       no settlement run exists for the requested key  404     false
STL-205       fill id reused with conflicting payload         409     false
STL-206       settle attempted against a finalized EOD run    409     false
STL-404       no route for method+path                        404     false
STL-999       unexpected internal error                       500     true
============  ==============================================  ======  =========

Every HTTP error response is the standard envelope produced by
:func:`error_envelope`::

    {"error": {"code", "message", "service", "retryable", "context"}}
"""

from typing import Any, Dict


class STLError(Exception):
    """Base exception for the settlement service."""

    code = "STL-000"
    retryable = False
    http_status = 500
    default_message = "Unspecified settlement error."

    def __init__(self, message: str = "", *, context: Dict[str, Any] | None = None) -> None:
        self.message = message or self.default_message
        self.context: Dict[str, Any] = dict(context or {})
        super().__init__(self.message)


class STLProtocolError(STLError):
    """STL-0xx: wire/protocol failures (malformed request body)."""

    code = "STL-001"
    retryable = False
    http_status = 400
    default_message = "Request body is not valid JSON."


class STLUpstreamError(STLError):
    """STL-0xx: an upstream dependency (S6 position keeper) could not be reached."""

    code = "STL-002"
    retryable = True
    http_status = 503
    default_message = "Position pull from the position keeper failed."


class STLConfigError(STLError):
    """STL-1xx: configuration problems, raised at boot before serving starts."""

    code = "STL-101"
    retryable = False
    http_status = 500
    default_message = "Settlement service is misconfigured."


class InvalidSettleRequest(STLError):
    """STL-2xx: a field in the POST /settle body failed validation.

    ``context`` always carries the offending ``field`` name (and, where useful,
    the raw value) so submitters can pinpoint their mistake.
    """

    code = "STL-201"
    retryable = False
    http_status = 400
    default_message = "The settle request contains an invalid or missing field."


class InvalidDateParam(STLError):
    """STL-2xx: a settlement-date parameter is malformed, past the epoch floor, or in the future."""

    code = "STL-202"
    retryable = False
    http_status = 400
    default_message = "The settlement date parameter is invalid."


class InvalidLimitParam(STLError):
    """STL-2xx: a ``limit`` query parameter is not a positive integer."""

    code = "STL-203"
    retryable = False
    http_status = 400
    default_message = "The limit query parameter must be a positive integer."


class UnknownRun(STLError):
    """STL-2xx: the requested settlement run (date) has no data."""

    code = "STL-204"
    retryable = False
    http_status = 404
    default_message = "No settlement run exists for the requested key."


class FillIdConflict(STLError):
    """STL-2xx: a fill id already recorded in this run appears again with different content.

    Re-submitting a *byte-identical* duplicate is an idempotent no-op (counted,
    reported as ``duplicate_fills_ignored``); reusing the same id for a *different*
    fill is ambiguous bookkeeping data and is rejected outright.
    """

    code = "STL-205"
    retryable = False
    http_status = 409
    default_message = "A fill with this id was already recorded with different content."


class RunFinalized(STLError):
    """STL-2xx: new settlement data arrived for a date whose EOD run is finalized.

    A finalized EOD report carries a content hash that must stay stable, so no
    further intraday fills or statement lines may mutate it.  The operator must
    settle the corrected day as a fresh revision or fix forward in a new period.
    """

    code = "STL-206"
    retryable = False
    http_status = 409
    default_message = "The settlement run for this date is finalized (EOD closed)."


class NoRouteError(STLError):
    """STL-4xx: no handler matches the request method+path.

    ``context`` carries ``method`` and ``path`` per CONVENTIONS §1.2 examples.
    """

    code = "STL-404"
    retryable = False
    http_status = 404
    default_message = "No route matches this request."


class STLErrorFallback(STLError):
    """STL-9xx: unexpected internal failure caught at the router boundary."""

    code = "STL-999"
    retryable = True
    http_status = 500
    default_message = "An unexpected internal error occurred."


def error_envelope(exc: STLError) -> Dict[str, Any]:
    """Serialize an :class:`STLError` to the standard §1.2 error envelope."""

    return {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "service": "settlement-service",
            "retryable": bool(exc.retryable),
            "context": dict(exc.context),
        }
    }


def make_error(code: str, message: str, *, status: int = 400, retryable: bool = False,
               context: Dict[str, Any] | None = None) -> STLError:
    """Build an :class:`STLError` carrying a specific code/message (used for NoRoute and the fallback)."""

    err = STLError(message, context=context)
    err.code = code  # type: ignore[misc]
    err.http_status = status  # type: ignore[misc]
    err.retryable = retryable  # type: ignore[misc]
    return err
