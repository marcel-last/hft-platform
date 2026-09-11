"""End-to-end HTTP tests for the dashboard handler layer.

A real ``ThreadingHTTPServer`` binds an *ephemeral* port (port=0); the auth
and gateway transports are scripted fakes, so no other process is needed and
no test ever opens a socket to the platform.
"""

import json
import threading
import time
import urllib.error
import urllib.request

from dash import __version__
from dash.clients import AuthClient, DashStats, GatewayClient
from dash.config import AuthConfig, DashConfig, GatewayConfig, ONE_HOUR_NS
from dash.controller import build_router
from dash.main import serve


def token_doc(n: int) -> bytes:
    return json.dumps({"token": "tok-%d" % n, "jti": "jti-%d" % n,
                       "sub": "dashboard", "scope": ["read", "write"],
                       "iat": 1, "nbf": 1, "exp": 1}).encode()


class FakeAuth:
    def __init__(self, cfg: AuthConfig):
        self.cfg = cfg
        self.calls = 0

    def __call__(self, method, url, body, headers, timeout_s):
        assert method == "POST" and url.endswith("/token")
        self.calls += 1
        return 200, token_doc(self.calls)


class FakeGateway:
    """Scripted by (method, path-prefix)."""

    def __init__(self):
        self.seen = []
        self.script = {}

    def __call__(self, method, url, body, headers, timeout_s):
        self.seen.append({"method": method, "url": url, "body": body,
                          "headers": headers})
        key = (method, url.split("?")[0])
        if key in self.script:
            r = self.script[key]
            if isinstance(r, list):
                return r.pop(0)
            return r
        return 404, (b'{"error":{"code":"PFA-404","message":"no such route",'
                     b'"service":"portfolio-analytics","retryable":false,'
                     b'"context":{}}}')


def start_server(fake_auth, fake_gw, read_only=False):
    cfg = DashConfig(bind="127.0.0.1", port=0, read_only=read_only,
                     auth=AuthConfig(), gateway=GatewayConfig())
    stats = DashStats()
    auth = AuthClient(cfg.auth, transport=fake_auth, stats=stats)
    gw = GatewayClient(cfg.gateway, auth, transport=fake_gw)
    httpd = serve(cfg, build_router(cfg, auth, gw, stats), auth, gw, stats)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, auth, gw


def http(port, method, path, body=None):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode())
        except ValueError:
            return e.code, {"raw": raw.decode("utf-8", "replace")}


def test_healthz_readyz_and_version():
    fa, fg = FakeAuth(AuthConfig()), FakeGateway()
    httpd, _, _ = start_server(fa, fg)
    try:
        port = httpd.server_address[1]
        status, body = http(port, "GET", "/healthz")
        assert status == 200
        assert body == {"status": "ok", "service": "dashboard",
                        "version": __version__}
        status, body = http(port, "GET", "/readyz")
        assert status == 200
        assert body["status"] in ("ready", "not_ready")  # no token yet
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_proxy_roundtrip_verbatim_with_query_and_token():
    fa, fg = FakeAuth(AuthConfig()), FakeGateway()
    fg.script[("GET", "http://127.0.0.1:7750/portfolio/pnl")] = (
        200, b'{"pnl":{"total":42.5},"window":"1d"}')
    httpd, auth, _ = start_server(fa, fg)
    try:
        port = httpd.server_address[1]
        status, body = http(port, "GET", "/api/portfolio/pnl?window=1d")
        assert status == 200
        assert body == {"pnl": {"total": 42.5}, "window": "1d"}
        req = fg.seen[-1]
        assert req["url"] == "http://127.0.0.1:7750/portfolio/pnl?window=1d"
        assert req["headers"]["Authorization"] == "Bearer tok-1"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_settle_forwards_body():
    fa, fg = FakeAuth(AuthConfig()), FakeGateway()
    fg.script[("POST", "http://127.0.0.1:7750/settlement/settle")] = (
        200, b'{"run_id":"R1","status":"settled"}')
    httpd, _, _ = start_server(fa, fg)
    try:
        port = httpd.server_address[1]
        status, body = http(port, "POST", "/api/settlement/settle",
                            body='{"date":"2026-09-10"}')
        assert status == 200
        assert body == {"run_id": "R1", "status": "settled"}
        assert fg.seen[-1]["body"] == b'{"date":"2026-09-10"}'
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_read_only_403_over_the_wire():
    fa, fg = FakeAuth(AuthConfig()), FakeGateway()
    httpd, _, _ = start_server(fa, fg, read_only=True)
    try:
        port = httpd.server_address[1]
        status, body = http(port, "POST", "/api/settlement/settle",
                            body='{"date":"2026-09-10"}')
        assert status == 403
        assert body["error"]["code"] == "UI-205"
        assert body["error"]["service"] == "dashboard"
        assert fg.seen == []  # nothing reached the "gateway"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_401_auto_refresh_over_the_wire():
    fa, fg = FakeAuth(AuthConfig()), FakeGateway()
    fg.script[("GET", "http://127.0.0.1:7750/portfolio/metrics")] = [
        (401, b'{"error":{"code":"API-401","message":"token rejected",'
              b'"service":"api-gateway","retryable":false,"context":{}}'),
        (200, b'{"sharpe":1.2}'),
    ]
    httpd, auth, _ = start_server(fa, fg)
    try:
        port = httpd.server_address[1]
        status, body = http(port, "GET", "/api/portfolio/metrics")
        assert status == 200
        assert body == {"sharpe": 1.2}
        # two gateway attempts, second carried the fresh token
        assert len(fg.seen) == 2
        assert fg.seen[0]["headers"]["Authorization"] == "Bearer tok-1"
        assert fg.seen[1]["headers"]["Authorization"] == "Bearer tok-2"
        assert fa.calls == 2  # initial + reactive re-fetch
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unknown_route_and_method_not_allowed():
    fa, fg = FakeAuth(AuthConfig()), FakeGateway()
    httpd, _, _ = start_server(fa, fg)
    try:
        port = httpd.server_address[1]
        status, body = http(port, "GET", "/definitely/not/here")
        assert status == 404 and body["error"]["code"] == "UI-404"
        status, body = http(port, "PUT", "/api/health")
        assert status == 405 and body["error"]["code"] == "UI-405"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_gateway_stats_endpoint_shape():
    fa, fg = FakeAuth(AuthConfig()), FakeGateway()
    fg.script[("GET", "http://127.0.0.1:7750/stats")] = (
        200, b'{"requests_total":3,"proxied":2}')
    httpd, auth, _ = start_server(fa, fg)
    try:
        port = httpd.server_address[1]
        status, body = http(port, "GET", "/api/gateway-stats")
        assert status == 200
        assert body["stats"] == {"requests_total": 3, "proxied": 2}
        assert "dashboard" in body and "auth" in body["dashboard"]
    finally:
        httpd.shutdown()
        httpd.server_close()
