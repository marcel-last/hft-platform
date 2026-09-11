"""Unit tests for dash.errors envelope shape and code classes."""

import dash.errors as E


def test_envelope_shape_is_exactly_the_documented_five_fields():
    exc = E.ReadOnlyViolationError("no writes", context={"route": "/x"})
    env = E.error_envelope(exc)
    assert set(env.keys()) == {"error"}
    err = env["error"]
    assert set(err.keys()) == {"code", "message", "service", "retryable",
                               "context"}
    assert err["code"] == "UI-205"
    assert err["service"] == "dashboard"
    assert err["retryable"] is False
    assert err["context"] == {"route": "/x"}


def test_all_codes_are_ui_nnn_format():
    import re
    for cls in (E.InternalError, E.MalformedBodyError, E.UnsupportedParamError,
                E.ReadOnlyViolationError, E.AuthUnreachableError,
                E.GatewayTransportError, E.TokenRefreshFailedError,
                E.UnknownRouteError, E.MethodNotAllowedError):
        assert re.fullmatch(r"UI-\d{3}", cls.code), cls.code


def test_retryable_split():
    # transport/upstream failures are retryable; request problems are not
    assert E.AuthUnreachableError.retryable is True
    assert E.GatewayTransportError.retryable is True
    assert E.TokenRefreshFailedError.retryable is True
    assert E.ReadOnlyViolationError.retryable is False
    assert E.MalformedBodyError.retryable is False
    assert E.UnknownRouteError.retryable is False
    assert E.MethodNotAllowedError.retryable is False


def test_status_split():
    assert E.AuthUnreachableError.status == 502
    assert E.GatewayTransportError.status == 502
    assert E.TokenRefreshFailedError.status == 502
    assert E.ReadOnlyViolationError.status == 403
    assert E.MalformedBodyError.status == 400
    assert E.UnknownRouteError.status == 404
    assert E.MethodNotAllowedError.status == 405
    assert E.InternalError.status == 500


def test_non_dash_error_falls_back_to_ui_001():
    env = E.error_envelope(ValueError("boom"))
    err = env["error"]
    assert err["code"] == "UI-001"
    assert err["retryable"] is False
    assert err["service"] == "dashboard"
    assert "ValueError" in err["context"]["exception"]


def test_message_override_and_default():
    e1 = E.MalformedBodyError()
    assert e1.code == "UI-201"
    assert e1.message  # non-empty default
    e2 = E.MalformedBodyError("custom sentence")
    assert e2.message == "custom sentence"


def test_error_response_tuple():
    status, body = E.error_response(E.AuthUnreachableError("down"))
    assert status == 502
    assert body["error"]["code"] == "UI-401"


def test_exception_is_catchable_as_dasherror():
    try:
        raise E.TokenRefreshFailedError("x")
    except E.DashError as caught:
        assert caught.code == "UI-403"
