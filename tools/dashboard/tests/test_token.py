"""Unit tests for AuthClient token lifecycle (fake transport, manual clock).

No network, no sleeping: the clock is an injected callable (CONVENTIONS §10
ManualClock pattern).
"""

import json

from dash.clients import AuthClient, DashStats, TransportFailure
from dash.config import AuthConfig, ONE_HOUR_NS
from dash.errors import AuthUnreachableError


class Clock:
    def __init__(self, t0: int = 1_000_000) -> None:
        self.t = t0

    def __call__(self) -> int:
        return self.t


def token_doc(n: int = 1) -> bytes:
    return json.dumps({"token": "tok-%d" % n, "jti": "jti-%d" % n,
                       "sub": "dashboard", "scope": ["read", "write"],
                       "iat": 1, "nbf": 1, "exp": 1}).encode()


def make_auth(responses=None, clock=None):
    """AuthClient with a scripted transport.

    ``responses`` is a list; each call pops one (int, bytes) or a
    TransportFailure. Returns (client, transport_log, stats, clock).
    """
    if responses is None:
        responses = [(200, token_doc(1))]
    log = []

    def transport(method, url, body, headers, timeout_s):
        log.append({"method": method, "url": url, "body": body,
                    "headers": dict(headers), "timeout_s": timeout_s})
        r = responses.pop(0)
        if isinstance(r, TransportFailure):
            raise r
        return r

    stats = DashStats()
    clock = clock or Clock()
    cfg = AuthConfig(token_ttl_ns=ONE_HOUR_NS)
    client = AuthClient(cfg, transport=transport, stats=stats, now_ns=clock)
    return client, log, stats, clock


def test_token_request_shape_and_caching():
    client, log, stats, _ = make_auth()
    t1 = client.get_token()
    t2 = client.get_token()
    assert t1 == "tok-1" == t2
    assert len(log) == 1  # second call served from cache
    sent = json.loads(log[0]["body"])
    assert sent["sub"] == "dashboard"
    assert sent["scopes"] == ["read", "write"]
    assert sent["ttl_ns"] == ONE_HOUR_NS
    assert log[0]["method"] == "POST"
    assert log[0]["url"] == "http://127.0.0.1:7720/token"
    assert stats.tokens_issued == 1
    assert client.current_jti() == "jti-1"


def test_proactive_refresh_after_75_percent_of_ttl():
    responses = [(200, token_doc(1)), (200, token_doc(2))]
    client, log, stats, clock = make_auth(responses)
    t1 = client.get_token()
    clock.t += int(0.74 * ONE_HOUR_NS)   # still above the watermark
    assert client.get_token() == t1
    assert len(log) == 1
    clock.t += int(0.02 * ONE_HOUR_NS)   # remaining < 25% -> re-fetch
    assert client.get_token() == "tok-2"
    assert len(log) == 2
    assert stats.tokens_issued == 2


def test_no_refresh_when_fresh():
    responses = [(200, token_doc(1))] * 5
    client, log, stats, clock = make_auth(responses)
    for _ in range(3):
        clock.t += int(0.1 * ONE_HOUR_NS)
        client.get_token()
    assert len(log) == 1


def test_reactive_refresh_bumps_counter_and_rotates_token():
    responses = [(200, token_doc(1)), (200, token_doc(2))]
    client, log, stats, _ = make_auth(responses)
    client.get_token()
    assert client.reactive_refresh() == "tok-2"
    assert stats.token_refreshes == 1
    assert stats.tokens_issued == 2
    assert client.current_jti() == "jti-2"


def test_ready_transitions():
    client, _, _, _ = make_auth()
    ready, _ = client.ready()
    assert ready is False
    client.get_token()
    ready, _ = client.ready()
    assert ready is True


def test_unreachable_auth_service():
    client, _, _, _ = make_auth([TransportFailure("http://127.0.0.1:7720/token",
                                                  "Connection refused")])
    try:
        client.get_token()
        assert False, "expected AuthUnreachableError"
    except AuthUnreachableError as e:
        assert e.code == "UI-401"
        assert e.retryable is True


def test_non_200_token_response():
    client, _, _, _ = make_auth([(500, b'{"error":{"code":"AUT-001"}}')])
    try:
        client.get_token()
        assert False, "expected AuthUnreachableError"
    except AuthUnreachableError as e:
        assert e.code == "UI-401"
        assert e.context["status"] == 500


def test_malformed_token_document():
    client, _, _, _ = make_auth([(200, b'{"nope": true}')])
    try:
        client.get_token()
        assert False, "expected AuthUnreachableError"
    except AuthUnreachableError:
        pass
