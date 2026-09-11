"""Unit tests for dash.controller route table + dispatch (fake clients).

No network: the gateway client is a stub object, the health probe is
injected, and token state comes from a stub auth client.
"""

import json

from dash.clients import DashStats, GatewayTransportError
from dash.config import DashConfig
from dash.controller import (api_gateway_stats, api_health, build_router,
                             build_route_table, dispatch_router, healthz,
                             readyz)


class StubAuth:
    def __init__(self, token="tok-1"):
        self.token = token

    def get_token(self):
        return self.token

    def reactive_refresh(self):
        self.token += "-r"
        return self.token

    def current_jti(self):
        return "jti-1"

    def ready(self):
        return True, "token active"


class StubGateway:
    def __init__(self, responses=None):
        self.responses = responses or []
        self.calls = []

    def proxy(self, method, path, query="", body=None):
        self.calls.append({"method": method, "path": path, "query": query,
                           "body": body})
        return self.responses.pop(0)

    def get_json(self, path, query=""):
        status, raw = self.proxy("GET", path, query)
        try:
            return status, json.loads(raw.decode()) if raw else {}
        except ValueError:
            return status, {"raw": raw.decode()}


def cfg(read_only=False):
    return DashConfig(read_only=read_only)


def route_table_map():
    return {(r.method, r.dash_path): r for r in build_route_table()}


def test_route_table_matches_plan():
    t = route_table_map()
    assert len(t) == 12
    # all six portfolio reads
    for p in ("/api/portfolio/pnl", "/api/portfolio/pnl/{symbol}",
              "/api/portfolio/metrics", "/api/portfolio/var",
              "/api/portfolio/attribution", "/api/portfolio/history"):
        assert ("GET", p) in t and t[("GET", p)].scope == "read"
    # settlement: 3 writes + 3 reads
    assert t[("POST", "/api/settlement/settle")].scope == "write"
    assert t[("POST", "/api/settlement/ingest")].scope == "write"
    assert t[("POST", "/api/settlement/finalize/{date}")].scope == "write"
    assert t[("GET", "/api/settlement/reports/{date}")].scope == "read"
    assert t[("GET", "/api/settlement/runs/{date}")].scope == "read"
    assert t[("GET", "/api/settlement/discrepancies")].scope == "read"
    assert t[("POST", "/api/settlement/settle")].body_required is True
    assert t[("POST", "/api/settlement/ingest")].body_required is True


def test_gateway_path_builders_strip_api_prefix():
    t = route_table_map()
    assert t[("GET", "/api/portfolio/pnl")].gw_builder({}) == "/portfolio/pnl"
    assert t[("GET", "/api/portfolio/pnl/{symbol}")].gw_builder(
        {"symbol": "FESX"}) == "/portfolio/pnl/FESX"
    assert t[("POST", "/api/settlement/finalize/{date}")].gw_builder(
        {"date": "2026-09-10"}) == "/settlement/finalize/2026-09-10"
    assert t[("GET", "/api/settlement/reports/{date}")].gw_builder(
        {"date": "2026-09-10"}) == "/settlement/reports/2026-09-10"
    assert t[("GET", "/api/settlement/runs/{date}")].gw_builder(
        {"date": "2026-09-10"}) == "/settlement/runs/2026-09-10"


def _dispatch(dash_method, dash_path, c, body=b"", query=None):
    return dispatch_router(build_router(c, StubAuth(), StubGateway(),
                                        DashStats()),
                           dash_method, dash_path, query or {}, body, c,
                           StubAuth(), StubGateway(), DashStats())


def test_healthz_and_readyz():
    c = cfg()
    assert healthz(c) == (200, {"status": "ok", "service": "dashboard",
                                "version": "1.0.0"})
    assert readyz(StubAuth())[0] == 200
    assert readyz(StubAuth()) == (200, {"status": "ready",
                                        "reasons": ["token active"]})


def test_404_and_405_envelopes():
    c = cfg()
    status, body = _dispatch("GET", "/api/unknown", c)
    assert status == 404 and body["error"]["code"] == "UI-404"
    status, body = _dispatch("DELETE", "/api/health", c)
    assert status == 405 and body["error"]["code"] == "UI-405"
    assert body["error"]["retryable"] is False
    assert "GET" in body["error"]["context"]["allowed"]


def test_read_only_blocks_writes_before_upstream():
    c = cfg(read_only=True)
    gw = StubGateway(responses=[])
    router = build_router(c, StubAuth(), gw, DashStats())
    status, body = dispatch_router(router, "POST", "/api/settlement/settle",
                                   {}, b'{"x":1}', c, StubAuth(), gw,
                                   DashStats())
    assert status == 403
    assert body["error"]["code"] == "UI-205"
    assert gw.calls == []  # no upstream traffic at all


def test_read_only_allows_reads():
    c = cfg(read_only=True)
    up = b'{"ok": true}'
    gw = StubGateway(responses=[(200, up)])
    router = build_router(c, StubAuth(), gw, DashStats())
    status, body = dispatch_router(router, "GET", "/api/portfolio/pnl",
                                   {}, b"", c, StubAuth(), gw, DashStats())
    assert status == 200 and body == {"ok": True}
    assert gw.calls[0]["path"] == "/portfolio/pnl"


def test_settle_without_body_is_ui_201():
    c = cfg()
    gw = StubGateway(responses=[])
    router = build_router(c, StubAuth(), gw, DashStats())
    status, body = dispatch_router(router, "POST", "/api/settlement/settle",
                                   {}, b"   ", c, StubAuth(), gw, DashStats())
    assert status == 400 and body["error"]["code"] == "UI-201"
    assert gw.calls == []


def test_query_forwarding_is_sorted_and_joined():
    c = cfg()
    gw = StubGateway(responses=[(200, b"{}")])
    router = build_router(c, StubAuth(), gw, DashStats())
    status, _ = dispatch_router(router, "GET", "/api/portfolio/history",
                                {"window": "1w", "metric": "pnl"}, b"", c,
                                StubAuth(), gw, DashStats())
    assert status == 200
    assert gw.calls[0]["query"] == "metric=pnl&window=1w"


def test_upstream_envelope_passthrough_not_rewritten():
    c = cfg()
    env = (b'{"error":{"code":"PFA-404","message":"unknown symbol",'
           b'"service":"portfolio-analytics","retryable":false,'
           b'"context":{}}}')
    gw = StubGateway(responses=[(404, env)])
    router = build_router(c, StubAuth(), gw, DashStats())
    status, body = dispatch_router(router, "GET",
                                   "/api/portfolio/pnl/NOPE", {}, b"", c,
                                   StubAuth(), gw, DashStats())
    assert status == 404
    assert body["error"]["code"] == "PFA-404"
    assert body["error"]["service"] == "portfolio-analytics"


def test_gateway_transport_error_becomes_ui_402():
    class BoomGateway(StubGateway):
        def proxy(self, method, path, query="", body=None):
            raise GatewayTransportError("unreachable", context={"peer": "x"})

    c = cfg()
    router = build_router(c, StubAuth(), BoomGateway(), DashStats())
    status, body = dispatch_router(router, "GET", "/api/portfolio/pnl",
                                   {}, b"", c, StubAuth(), BoomGateway(),
                                   DashStats())
    assert status == 502 and body["error"]["code"] == "UI-402"
    assert body["error"]["retryable"] is True


def test_api_gateway_stats_wraps_verbatim():
    c = cfg(read_only=True)
    gw = StubGateway(responses=[(200, json.dumps({"requests_total": 9,
                                                  "proxied": 7}).encode())])
    stats = DashStats()
    stats.bump_issued("j1", 123)
    status, body = api_gateway_stats(c, gw, stats)
    assert status == 200
    assert body["stats"] == {"requests_total": 9, "proxied": 7}
    assert body["dashboard"]["read_only"] is True
    assert body["dashboard"]["auth"]["tokens_issued"] == 1
    assert body["dashboard"]["auth"]["last_token_ok_jti"] == "j1"


def test_api_gateway_stats_verbatim_on_error():
    c = cfg()
    env = b'{"error":{"code":"API-503","message":"nope","service":"api-gateway",' \
          b'"retryable":true,"context":{}}}'
    gw = StubGateway(responses=[(503, env)])
    status, body = api_gateway_stats(c, gw, DashStats())
    assert status == 503
    assert body["error"]["code"] == "API-503"


def _probe_factory(overrides):
    def probe(host, svc, timeout_ms):
        base = {"name": svc.name, "port": svc.port, "package": svc.package,
                "live": False, "healthz": "unreachable",
                "healthz_version": None, "ready": None, "ready_reasons": [],
                "latency_ms": None, "error": None}
        base.update(overrides.get(svc.port, {}))
        return base
    return probe


def test_health_sweep_summary_and_order():
    c = cfg()
    up = {7610: {"live": True, "healthz": "ok", "healthz_version": "1.0.0",
                 "ready": "ready", "latency_ms": 2},
          7620: {"live": True, "healthz": "ok", "healthz_version": "1.0.0",
                 "ready": "not_ready", "ready_reasons": ["books empty"],
                 "latency_ms": 4},
          7750: {"live": True, "healthz": "ok", "healthz_version": "1.0.0",
                 "ready": "ready", "latency_ms": 1}}
    status, body = api_health(c, probe=_probe_factory(up))
    assert status == 200
    svcs = body["services"]
    assert [s["port"] for s in svcs] == list(range(7610, 7760, 10))
    assert svcs[0]["name"] == "market-data-gateway"
    assert svcs[-1]["name"] == "api-gateway"
    s = body["summary"]
    assert s["live"] == 3 and s["down"] == 12 and s["not_ready"] == 1
    assert s["checked"] == 15 and s["read_only"] is False
    assert s["elapsed_ms"] >= 0


def test_health_sweep_counts_not_ready_and_read_only():
    c = cfg(read_only=True)
    up = {p: {"live": True, "healthz": "ok", "healthz_version": "1.0.0",
              "ready": "ready"} for p in range(7610, 7760, 10)}
    up[7690]["ready"] = "not_ready"
    up[7690]["ready_reasons"] = ["no data"]
    status, body = api_health(c, probe=_probe_factory(up))
    assert body["summary"]["live"] == 15
    assert body["summary"]["not_ready"] == 1
    assert body["summary"]["read_only"] is True
