"""Unit tests for dash.router (no network)."""

from dash.router import Router, Found, MethodNotAllowed, NotFound


def make_router() -> Router:
    def pnl(q, b, p):
        return 200, {"ok": True, "symbol": p.get("symbol")}

    r = Router()
    r.add("GET", "/api/portfolio/pnl", "pnl")
    r.add("GET", "/api/portfolio/pnl/{symbol}", pnl)
    r.add("POST", "/api/settlement/settle", "settle")
    r.add("GET", "/healthz", "healthz")
    return r


def test_exact_match_no_params():
    r = make_router()
    res = r.resolve("GET", "/api/portfolio/pnl")
    assert isinstance(res, Found)
    assert res.name == "pnl"
    assert res.params == {}


def test_param_capture():
    r = make_router()
    res = r.resolve("GET", "/api/portfolio/pnl/EU_STOXX50_CONT")
    assert isinstance(res, Found)
    assert res.name == "pnl"
    assert res.params == {"symbol": "EU_STOXX50_CONT"}
    assert res.handler is not None
    status, body = res.handler({}, b"", res.params)
    assert status == 200 and body["symbol"] == "EU_STOXX50_CONT"


def test_method_not_allowed_reports_allowed_set():
    r = make_router()
    res = r.resolve("DELETE", "/api/portfolio/pnl")
    assert isinstance(res, MethodNotAllowed)
    assert res.allowed == ("GET",)
    res2 = r.resolve("PUT", "/api/settlement/settle")
    assert isinstance(res2, MethodNotAllowed)
    assert res2.allowed == ("POST",)


def test_not_found():
    r = make_router()
    assert isinstance(r.resolve("GET", "/nope"), NotFound)
    assert isinstance(r.resolve("PATCH", "/nope"), NotFound)
    # a param route does not leak to a wrong segment count
    assert isinstance(r.resolve("GET", "/api/portfolio/pnl/a/b"), NotFound)


def test_query_string_is_stripped_before_matching():
    r = make_router()
    res = r.resolve("GET", "/api/portfolio/pnl?window=1d")
    assert isinstance(res, Found) and res.name == "pnl"
    res2 = r.resolve("GET", "/api/portfolio/pnl/FESX?limit=5")
    assert isinstance(res2, Found)
    assert res2.params == {"symbol": "FESX"}


def test_trailing_slash_is_tolerated():
    r = make_router()
    assert isinstance(r.resolve("GET", "/healthz/"), Found)


def test_param_does_not_cross_slash():
    r = make_router()
    # [^/]+ must not swallow the next path segment
    assert isinstance(r.resolve("GET", "/api/portfolio/pnl/a/b"), NotFound)


def test_registration_order_and_names():
    r = make_router()
    assert r.route_names() == ["pnl", "pnl", "settle", "healthz"]


def test_param_names_visible_in_groupdict():
    r = Router()
    r.add("GET", "/api/settlement/finalize/{date}", "fin")
    res = r.resolve("GET", "/api/settlement/finalize/2026-09-10")
    assert isinstance(res, Found)
    assert res.params == {"date": "2026-09-10"}


def test_add_accepts_callables_and_strings():
    def h(q, b, p):
        return 200, {}

    r = Router()
    r.add("GET", "/a", h).add("GET", "/b", "named")
    res_a = r.resolve("GET", "/a")
    res_b = r.resolve("GET", "/b")
    assert res_a.handler is h
    assert res_b.handler is None and res_b.name == "named"
