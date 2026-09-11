"""Unit tests for GatewayClient: verbatim passthrough + 401 auto-refresh."""

import json

from dash.clients import (AuthClient, DashStats, GatewayClient,
                          TransportFailure)
from dash.config import AuthConfig, GatewayConfig, ONE_HOUR_NS
from dash.errors import (AuthUnreachableError, GatewayTransportError,
                         TokenRefreshFailedError)


class Clock:
    def __init__(self, t0: int = 1_000_000) -> None:
        self.t = t0

    def __call__(self) -> int:
        return self.t


def token_doc(n: int) -> bytes:
    return json.dumps({"token": "tok-%d" % n, "jti": "jti-%d" % n,
                       "sub": "dashboard", "scope": ["read", "write"],
                       "iat": 1, "nbf": 1, "exp": 1}).encode()


def make_gateway(gw_responses, auth_responses=None):
    """(client, auth_log, gw_log, stats, clock).

    ``gw_responses``: list consumed by the gateway transport; entries are
    (status, bytes) or TransportFailure.
    """
    if auth_responses is None:
        auth_responses = [(200, token_doc(1)), (200, token_doc(2)),
                          (200, token_doc(3))]
    auth_log, gw_log = [], []

    def auth_transport(method, url, body, headers, timeout_s):
        auth_log.append({"url": url, "body": body})
        r = auth_responses.pop(0)
        if isinstance(r, TransportFailure):
            raise r
        return r

    def gw_transport(method, url, body, headers, timeout_s):
        gw_log.append({"method": method, "url": url, "body": body,
                       "headers": dict(headers)})
        r = gw_responses.pop(0)
        if isinstance(r, TransportFailure):
            raise r
        return r

    stats = DashStats()
    clock = Clock()
    auth = AuthClient(AuthConfig(), transport=auth_transport, stats=stats,
                      now_ns=clock)
    client = GatewayClient(GatewayConfig(), auth, transport=gw_transport)
    return client, auth_log, gw_log, stats, clock


def test_verbatim_200_passthrough_with_bearer_and_query():
    up = json.dumps({"pnl": {"total": 1.5}}).encode()
    client, auth_log, gw_log, _, _ = make_gateway([(200, up)])
    status, body = client.proxy("GET", "/portfolio/pnl", "window=1d")
    assert status == 200
    assert body == up  # verbatim bytes, untouched
    req = gw_log[0]
    assert req["url"] == "http://127.0.0.1:7750/portfolio/pnl?window=1d"
    assert req["headers"]["Authorization"] == "Bearer tok-1"
    assert req["body"] is None
    assert len(auth_log) == 1  # token fetched once


def test_error_envelopes_pass_through_verbatim():
    payloads = [
        (404, ('{"error":{"code":"PFA-404","message":"no such symbol",'
               '"service":"portfolio-analytics","retryable":false,'
               '"context":{}}').encode()),
        (403, ('{"error":{"code":"API-403","message":"scope denied",'
               '"service":"api-gateway","retryable":false,'
               '"context":{}}').encode()),
        (503, ('{"error":{"code":"STL-503","message":"upstream down",'
               '"service":"settlement-service","retryable":true,'
               '"context":{}}').encode()),
    ]
    for status, payload in payloads:
        client, _, gw_log, _, _ = make_gateway([(status, payload)])
        out_status, out_body = client.proxy("POST", "/settlement/settle",
                                            "", b'{"a":1}')
        assert out_status == status
        assert out_body == payload  # envelope untouched
        assert len(gw_log) == 1
        assert gw_log[0]["headers"]["Content-Length"] == str(len(b'{"a":1}'))


def test_401_triggers_exactly_one_refetch_and_retry():
    up = json.dumps({"ok": True}).encode()
    client, auth_log, gw_log, stats, _ = make_gateway(
        [(401, b'{"error":{"code":"API-401"}}'), (200, up)])
    status, body = client.proxy("GET", "/portfolio/pnl")
    assert status == 200
    assert body == up
    assert len(auth_log) == 2  # initial + reactive re-fetch
    assert stats.token_refreshes == 1
    assert stats.tokens_issued == 2
    assert len(gw_log) == 2
    # first attempt carried tok-1, retry carried the fresh tok-2
    assert gw_log[0]["headers"]["Authorization"] == "Bearer tok-1"
    assert gw_log[1]["headers"]["Authorization"] == "Bearer tok-2"


def test_second_401_passes_through_verbatim():
    body401 = b'{"error":{"code":"API-401","message":"token rejected",'
    client, auth_log, gw_log, _, _ = make_gateway(
        [(401, body401), (401, body401)])
    status, body = client.proxy("GET", "/portfolio/metrics")
    assert status == 401
    assert body == body401
    assert len(gw_log) == 2      # only one retry
    assert len(auth_log) == 2    # only one re-fetch


def test_refresh_failure_raises_ui_403():
    client, auth_log, gw_log, _, _ = make_gateway(
        [(401, b'{"error":{}}')],
        auth_responses=[(200, token_doc(1)),
                        TransportFailure("http://127.0.0.1:7720/token",
                                         "refused")])
    try:
        client.proxy("GET", "/portfolio/pnl")
        assert False, "expected TokenRefreshFailedError"
    except TokenRefreshFailedError as e:
        assert e.code == "UI-403"
        assert e.retryable is True


def test_gateway_transport_failure_raises_ui_402():
    client, _, gw_log, _, _ = make_gateway(
        [TransportFailure("http://127.0.0.1:7750/portfolio/pnl", "timeout")])
    try:
        client.proxy("GET", "/portfolio/pnl")
        assert False, "expected GatewayTransportError"
    except GatewayTransportError as e:
        assert e.code == "UI-402"
        assert e.retryable is True


def test_get_json_parses_and_wraps_non_json():
    client, _, _, _, _ = make_gateway([(200, b'{"requests_total": 7}')])
    status, doc = client.get_json("/stats")
    assert status == 200 and doc == {"requests_total": 7}
    client2, _, _, _, _ = make_gateway([(200, b"not-json")])
    status2, doc2 = client2.get_json("/stats")
    assert status2 == 200
    assert doc2 == {"raw": "not-json"}
    client3, _, _, _, _ = make_gateway([(200, b"")])
    status3, doc3 = client3.get_json("/stats")
    assert status3 == 200 and doc3 == {}
