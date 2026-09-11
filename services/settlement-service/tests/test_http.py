"""S14 Milestone 2 — full HTTP end-to-end tests.

Runs the real service HTTP stack (main.serve_http + ThreadingHTTPServer) on an
ephemeral 127.0.0.1:0 port — no sleeps, ManualClock time, no real S6 (the
controller is constructed with ``ingest_client=None`` or a stub for a
deterministic readyz).  Every assertion checks BOTH the exact §1.2 envelope
shape/content AND the HTTP status (S13 lesson).
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request

import pytest

from stls.config import MatchingConfig, SettlementConfig
from stls.controller import SettlementController
from stls.main import serve_http
from stls.models import ManualClock
from stls.router import build_router
from stls.settlement_engine import SettlementEngine

FIX_DAY = "2026-09-10"
FIX_NS = int(dt.datetime(2026, 9, 10, 12, tzinfo=dt.timezone.utc).timestamp() * 1e9)


def make_cfg(**overrides) -> SettlementConfig:
    import dataclasses
    base = SettlementConfig()
    for name, value in overrides.items():
        base = dataclasses.replace(
            base, **{name: dataclasses.replace(getattr(base, name), **value)})
    return base


class StubIngest:
    """Deterministic stand-in for PositionKeeperClient (no real S6 in tests).

    Mirrors the real client's counter semantics: cumulative successes/failures
    plus ``consecutive_failures`` (0 while healthy, reset on a successful pull).
    """

    def __init__(self, rows=None, fail=False) -> None:
        self.rows = rows
        self.fail = fail
        self.pull_failures = 0
        self.pull_successes = 0
        self.consecutive_failures = 0

    def pull_positions(self):
        if self.fail:
            self.pull_failures += 1
            self.consecutive_failures += 1
            return None
        self.pull_successes += 1
        self.consecutive_failures = 0
        return list(self.rows or [])


def make_harness(ingest_client=None, **cfg_overrides):
    cfg = make_cfg(**cfg_overrides)
    clock = ManualClock(FIX_NS)
    engine = SettlementEngine(cfg, clock=clock)
    controller = SettlementController(cfg=cfg, engine=engine,
                                      ingest_client=ingest_client)
    router = build_router(controller)
    httpd = serve_http(router, controller, "127.0.0.1", 0)
    host, port = httpd.server_address[:2]
    return {"httpd": httpd, "base": f"http://{host}:{port}", "cfg": cfg,
            "engine": engine, "controller": controller, "client": ingest_client,
            "port": port}


def req(harness, method, path, body=None, raw_body=None):
    """Issue one HTTP request; return (status, parsed_json_or_None, raw_text)."""
    url = harness["base"] + path
    data = None
    headers = {"Accept": "application/json"}
    if raw_body is not None:
        data = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
        headers["Content-Type"] = "application/json"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, (json.loads(text) if text else None), text
    except urllib.error.HTTPError as err:
        text = err.read().decode("utf-8")
        return err.code, (json.loads(text) if text else None), text


ENVELOPE_STATUS = {
    "STL-001": 400, "STL-201": 400, "STL-202": 400, "STL-203": 400,
    "STL-204": 404, "STL-404": 404, "STL-405": 405,
    "STL-205": 409, "STL-206": 409, "STL-002": 503,
}


def assert_envelope(status, body, code, message_prefix="", retryable=False,
                    context_keys=()):
    """Assert the exact §1.2 error envelope AND the HTTP status (S13 lesson)."""
    assert status == ENVELOPE_STATUS[code], f"status {status} != {ENVELOPE_STATUS[code]}"
    assert isinstance(body, dict) and set(body) == {"error"}
    err = body["error"]
    assert err["code"] == code
    assert err["service"] == "settlement-service"
    assert err["retryable"] is retryable
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["context"], dict)
    for key in context_keys:
        assert key in err["context"], f"missing context key {key!r}"
    if message_prefix:
        assert err["message"].startswith(message_prefix)
    return err


def fill(fill_id="F1", venue="EUREX", symbol="FESX", side="BUY", px=50.0,
         qty=1, fee=0.0, account="MAIN", ts_ns=FIX_NS):
    return {"fill_id": fill_id, "ts_ns": ts_ns, "venue": venue, "symbol": symbol,
            "side": side, "px": px, "qty": qty, "fee": fee, "account": account}


def stmt(venue="EUREX", symbol="FESX", side="BUY", px=50.0, qty=1, fee=0.0,
         stmt_line_id=None, ts_ns=None):
    return {"venue": venue, "symbol": symbol, "side": side, "px": px, "qty": qty,
            "fee": fee, "stmt_line_id": stmt_line_id, "ts_ns": ts_ns}


@pytest.fixture
def harness():
    h = make_harness()
    yield h
    h["httpd"].shutdown()
    h["httpd"].server_close()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_healthz(harness):
    status, body, text = req(harness, "GET", "/healthz")
    assert status == 200
    assert json.loads(text) == body
    assert body == {"status": "ok", "service": "settlement-service",
                    "version": "1.0.0"}


def test_readyz_no_ingest_client_is_ready(harness):
    status, body, _ = req(harness, "GET", "/readyz")
    assert status == 200
    assert body == {"status": "ready", "reasons": []}


def test_readyz_reflects_pull_failures_via_stub():
    stub = StubIngest(rows=[{"symbol": "FESX", "account": "MAIN", "net_qty": 3,
                             "avg_price": 55.0}], fail=True)
    h = make_harness(ingest_client=stub)
    try:
        # A failed manual pull flips readyz deterministically (no real S6).
        status, body, _ = req(h, "POST", "/ingest")
        assert_envelope(status, body, "STL-002", retryable=True)
        status, body, _ = req(h, "GET", "/readyz")
        assert status == 200
        assert body["status"] == "not_ready"
        assert any("pull failures" in r for r in body["reasons"])
        # A successful pull restores readiness.
        stub.fail = False
        status, body, _ = req(h, "POST", "/ingest")
        assert status == 200 and body["ingested"] is True
        status, body, _ = req(h, "GET", "/readyz")
        assert body == {"status": "ready", "reasons": []}
    finally:
        h["httpd"].shutdown()
        h["httpd"].server_close()


# ---------------------------------------------------------------------------
# POST /settle — happy path, replay, conflict, errors
# ---------------------------------------------------------------------------

def test_settle_happy_path(harness):
    body = {"date": FIX_DAY,
            "fills": [fill("F1", px=50.0, qty=3, fee=1.0),
                      fill("F2", side="SELL", px=52.0, qty=1, fee=0.5)],
            "statement_lines": [stmt("EUREX", "FESX", px=50.5, qty=4)]}
    status, out, text = req(harness, "POST", "/settle", body=body)
    assert status == 200
    assert json.loads(text) == out
    assert out["date"] == FIX_DAY and out["run_kind"] == "intraday"
    assert out["finalized"] is False
    assert out["fills_added"] == 2 and out["duplicate_fills_ignored"] == 0
    assert out["statement_venues_replaced"] == ["EUREX"]
    run = out["run"]
    assert run["fill_count"] == 2 and run["statement_line_count"] == 1
    assert run["settle_calls"] == 1 and run["content_hash"] is None
    # Venue/symbol are normalized to upper case in the stored run.
    assert [f.venue for f in harness["engine"]._runs[FIX_DAY].fills] == \
        ["EUREX", "EUREX"]


def test_settle_idempotent_replay(harness):
    body = {"date": FIX_DAY, "fills": [fill("F1"), fill("F2", symbol="ES")],
            "statement_lines": [stmt("EUREX", "FESX")]}
    status, first, _ = req(harness, "POST", "/settle", body=body)
    assert status == 200 and first["fills_added"] == 2
    status, replay, _ = req(harness, "POST", "/settle", body=body)
    assert status == 200
    assert replay["fills_added"] == 0 and replay["duplicate_fills_ignored"] == 2
    assert replay["run"]["fill_count"] == 2
    assert replay["run"]["duplicate_fills_ignored"] == 2
    assert replay["run"]["settle_calls"] == 2


def test_settle_fill_id_conflict_409(harness):
    status, _, _ = req(harness, "POST", "/settle",
                       body={"date": FIX_DAY, "fills": [fill("F1", px=50.0)]})
    assert status == 200
    status, body, _ = req(harness, "POST", "/settle",
                          body={"date": FIX_DAY, "fills": [fill("F1", px=51.0)]})
    assert_envelope(status, body, "STL-205",
                    message_prefix="fill_id 'F1' was already recorded",
                    context_keys=("fill_id", "date"))
    assert body["error"]["context"]["fill_id"] == "F1"
    assert body["error"]["context"]["date"] == FIX_DAY


def test_settle_bad_body_400(harness):
    cases = [
        ({"date": "2030-01-01", "fills": []}, "STL-202"),          # future date
        ({"date": "garbage", "fills": []}, "STL-202"),             # bad date
        ({"fills": []}, "STL-202"),                                # missing date
        ({"date": FIX_DAY, "fills": [{"fill_id": "F1", "ts_ns": 1,
                                      "venue": "V", "symbol": "S",
                                      "side": "HOLD", "px": 1, "qty": 1}]},
         "STL-201"),                                              # bad side
        ({"date": FIX_DAY, "fills": [{"fill_id": "F1", "ts_ns": 1,
                                      "venue": "V", "symbol": "S",
                                      "px": 1, "qty": 0}]}, "STL-201"),
        ({"date": FIX_DAY, "fills": "nope"}, "STL-201"),           # non-array
        ({"date": FIX_DAY, "fills": [fill("F1"), 7]}, "STL-201"),  # non-object
    ]
    for payload, code in cases:
        status, body, _ = req(harness, "POST", "/settle", body=payload)
        assert_envelope(status, body, code)
    # A rejected body never creates a run.
    status, out, _ = req(harness, "GET", "/runs/2020-01-01")
    assert_envelope(status, out, "STL-204")


def test_settle_missing_body_and_bad_json(harness):
    status, body, _ = req(harness, "POST", "/settle")  # no body at all
    assert_envelope(status, body, "STL-201", context_keys=("field",))
    assert body["error"]["context"]["field"] == "$"
    status, body, _ = req(harness, "POST", "/settle", raw_body="{not json")
    assert_envelope(status, body, "STL-001")
    assert body["error"]["retryable"] is False


# ---------------------------------------------------------------------------
# POST /finalize/{date}
# ---------------------------------------------------------------------------

def test_finalize_happy_idempotent_and_post_seal_409(harness):
    req(harness, "POST", "/settle",
        body={"date": FIX_DAY, "fills": [fill("F1", px=50.0, qty=2, fee=0.25)],
              "statement_lines": [stmt("EUREX", "FESX", px=50.0, qty=2)]})
    status, seal, _ = req(harness, "POST", f"/finalize/{FIX_DAY}")
    assert status == 200
    assert seal["date"] == FIX_DAY and seal["finalized"] is True
    assert seal["idempotent"] is False
    assert isinstance(seal["content_hash"], str) and len(seal["content_hash"]) == 64
    assert seal["run"]["kind"] == "eod" and seal["run"]["finalized"] is True

    # Re-finalize is idempotent with the identical seal.
    status, again, _ = req(harness, "POST", f"/finalize/{FIX_DAY}")
    assert status == 200 and again["idempotent"] is True
    assert again["content_hash"] == seal["content_hash"]

    # Post-seal mutation is a 409 STL-206.
    status, body, _ = req(harness, "POST", "/settle",
                          body={"date": FIX_DAY, "fills": [fill("F9")]})
    assert_envelope(status, body, "STL-206",
                    context_keys=("date", "content_hash"))
    assert body["error"]["context"]["content_hash"] == seal["content_hash"]

    # Sealing an unknown date is 404 STL-204; a bad date is 400 STL-202.
    status, body, _ = req(harness, "POST", "/finalize/2020-01-01")
    assert_envelope(status, body, "STL-204")
    status, body, _ = req(harness, "POST", "/finalize/nonsense")
    assert_envelope(status, body, "STL-202")
    # GET on the parameterized route is 405 (allowed: POST).
    status, body, _ = req(harness, "GET", f"/finalize/{FIX_DAY}")
    assert_envelope(status, body, "STL-405")
    assert body["error"]["context"]["allowed"] == ["POST"]


# ---------------------------------------------------------------------------
# GET /reports/{date} and GET /runs/{date}
# ---------------------------------------------------------------------------

def seed(harness, date=FIX_DAY):
    """A day with 2 venues, one missing statement, one qty mismatch."""
    body = {"date": date,
            "fills": [fill("F1", venue="CME", symbol="ES", px=5000.0, qty=3, fee=2.0),
                      fill("F2", venue="EUREX", symbol="FESX", px=50.0, qty=4, fee=1.0),
                      fill("F3", venue="EUREX", symbol="FESX", side="SELL",
                           px=52.0, qty=1, fee=1.0)],
            "statement_lines": [stmt("EUREX", "FESX", px=50.5, qty=5)]}
    status, out, _ = req(harness, "POST", "/settle", body=body)
    assert status == 200
    return out


def test_report_shape_and_math(harness):
    seed(harness)
    status, rep, text = req(harness, "GET", f"/reports/{FIX_DAY}")
    assert status == 200 and json.loads(text) == rep
    assert rep["run"]["fill_count"] == 3
    assert rep["run"]["content_hash"] is None and rep["verify_hash"] is None

    pos = {(p["account"], p["symbol"]): p for p in rep["net_positions"]}
    assert set(pos) == {("MAIN", "ES"), ("MAIN", "FESX")}
    assert pos[("MAIN", "FESX")]["net_qty"] == 3
    assert pos[("MAIN", "ES")]["buy_notional"] == 15000.0

    cash = rep["cash_summary"]
    assert cash["buy_notional"] == 15200.0 and cash["sell_notional"] == 52.0
    assert cash["total_fees"] == 4.0
    assert cash["net_cash"] == 52.0 - 15200.0 - 4.0
    assert cash["fill_count"] == 3

    assert [d["kind"] for d in rep["discrepancies"]] == [
        "MISSING_STATEMENT", "QTY_MISMATCH"]
    assert rep["discrepancy_count"] == 2
    qty = [d for d in rep["discrepancies"] if d["kind"] == "QTY_MISMATCH"][0]
    assert (qty["venue"], qty["symbol"]) == ("EUREX", "FESX")
    assert (qty["our_qty"], qty["stmt_qty"], qty["qty_delta"]) == (3, 5, -2)
    assert qty["severity"] == "CRITICAL"
    assert qty["detected_ns"] == FIX_NS  # clock-stamped per read
    assert len(rep["revisions"]) == 1
    assert rep["revisions"][0]["action"] == "settle"
    assert rep["revisions"][0]["fills_added"] == 3

    # Sealing publishes a stable verify_hash that equals the seal digest.
    status, seal, _ = req(harness, "POST", f"/finalize/{FIX_DAY}")
    assert status == 200
    status, rep2, _ = req(harness, "GET", f"/reports/{FIX_DAY}")
    assert rep2["verify_hash"] == seal["content_hash"]
    status, rep3, _ = req(harness, "GET", f"/reports/{FIX_DAY}")
    assert rep3["verify_hash"] == rep2["verify_hash"]  # stable across reads


def test_reports_unknown_and_bad_date(harness):
    status, body, _ = req(harness, "GET", "/reports/2020-01-01")
    assert_envelope(status, body, "STL-204")
    status, body, _ = req(harness, "GET", "/reports/nope")
    assert_envelope(status, body, "STL-202")
    status, body, _ = req(harness, "GET", "/reports/2031-01-01")
    assert_envelope(status, body, "STL-202")


def test_runs_status(harness):
    seed(harness)
    status, out, _ = req(harness, "GET", f"/runs/{FIX_DAY}")
    assert status == 200
    run = out["run"]
    assert run["date"] == FIX_DAY and run["kind"] == "intraday"
    assert run["finalized"] is False
    assert run["fill_count"] == 3 and run["statement_line_count"] == 1
    assert run["settle_calls"] == 1 and run["duplicate_fills_ignored"] == 0
    assert run["created_ns"] == FIX_NS and run["updated_ns"] == FIX_NS
    status, body, _ = req(harness, "GET", "/runs/2020-01-01")
    assert_envelope(status, body, "STL-204")


# ---------------------------------------------------------------------------
# GET /discrepancies (filters + limit)
# ---------------------------------------------------------------------------

def test_discrepancies_filters(harness):
    seed(harness)  # CME/ES MISSING; EUREX/FESX QTY
    req(harness, "POST", "/settle",
        body={"date": "2026-09-09", "fills": [fill("G1", venue="NYAR", symbol="NQ", px=200.0, qty=2)],
              "statement_lines": [stmt("CME", "ES", px=1.0, qty=1)]})
    # 2026-09-09: NYAR/NQ MISSING + CME/ES UNEXPECTED.

    status, out, _ = req(harness, "GET", "/discrepancies")
    assert status == 200
    kinds = [d["kind"] for d in out["discrepancies"]]
    assert out["count"] == 4 and out["truncated"] is False
    # Newest date first; within a date, sorted by (venue, symbol, kind).
    assert kinds == ["MISSING_STATEMENT", "QTY_MISMATCH",
                     "UNEXPECTED_SYMBOL", "MISSING_STATEMENT"]

    status, out, _ = req(harness, "GET", f"/discrepancies?date={FIX_DAY}")
    assert out["count"] == 2
    status, out, _ = req(harness, "GET", f"/discrepancies?date={FIX_DAY}&venue=cme")
    assert out["count"] == 1 and out["discrepancies"][0]["kind"] == "MISSING_STATEMENT"
    status, out, _ = req(harness, "GET", f"/discrepancies?date={FIX_DAY}&kind=qty_mismatch")
    assert out["count"] == 1 and out["discrepancies"][0]["venue"] == "EUREX"
    status, out, _ = req(harness, "GET", "/discrepancies?limit=3")
    assert out["count"] == 3 and out["truncated"] is True
    status, out, _ = req(harness, "GET", "/discrepancies?limit=500")
    assert out["count"] == 4 and out["truncated"] is False

    # A bad date filter is 400; a bad limit is 400 STL-203.
    status, body, _ = req(harness, "GET", "/discrepancies?date=nope")
    assert_envelope(status, body, "STL-202")
    # "" is the regression case: ?limit= must reach the handler as an empty
    # string (keep_blank_values) and be rejected, not silently default.
    for bad in ("zero", "-1", "0", "2.5", ""):
        status, body, _ = req(harness, "GET", f"/discrepancies?limit={bad}")
        assert_envelope(status, body, "STL-203", context_keys=("raw",))


# ---------------------------------------------------------------------------
# GET /stats
# ---------------------------------------------------------------------------

def test_stats_over_http(harness):
    status, s0, _ = req(harness, "GET", "/stats")
    assert status == 200
    assert s0["run_count"] == 0 and s0["settle_calls"] == 0
    assert s0["dates"] == [] and s0["runs"] == {}
    assert s0["ingest"] == {"enabled": False, "pull_successes": 0,
                            "pull_failures": 0}

    seed(harness)
    req(harness, "POST", "/settle",
        body={"date": FIX_DAY, "fills": [fill("F1", venue="CME", symbol="ES",
                                              px=5000.0, qty=3, fee=2.0)]})
    req(harness, "POST", f"/finalize/{FIX_DAY}")
    status, s, _ = req(harness, "GET", "/stats")
    assert status == 200
    assert s["run_count"] == 1 and s["finalized_runs"] == 1
    assert s["settle_calls"] == 2 and s["duplicate_fills_ignored"] == 1
    assert s["fill_id_conflicts"] == 0 and s["dates"] == [FIX_DAY]
    assert s["runs"][FIX_DAY]["kind"] == "eod"
    assert s["runs"][FIX_DAY]["fill_count"] == 3  # seed 3 + dup replay adds 0


# ---------------------------------------------------------------------------
# Structured 404 / 405 (S13 lesson: exact §1.2 envelope + status)
# ---------------------------------------------------------------------------

def test_404_no_route_with_method_path_context(harness):
    status, body, _ = req(harness, "GET", "/nope")
    assert_envelope(status, body, "STL-404",
                    context_keys=("method", "path"))
    assert body["error"]["context"] == {"method": "GET", "path": "/nope"}
    assert status == 404

    # DELETE on an existing path is 405 (method not allowed), not 404.
    status, body, _ = req(harness, "DELETE", "/settle")
    assert_envelope(status, body, "STL-405")
    assert body["error"]["context"]["method"] == "DELETE"
    assert body["error"]["context"]["allowed"] == ["POST"]

    # Deeper unknown paths do not accidentally match a parameter route.
    status, body, _ = req(harness, "GET", "/reports/2020-01-01/extra")
    assert_envelope(status, body, "STL-404")


def test_405_with_allowed_list(harness):
    status, body, _ = req(harness, "GET", "/settle")
    assert_envelope(status, body, "STL-405")
    assert status == 405
    assert body["error"]["context"]["allowed"] == ["POST"]
    assert body["error"]["context"]["method"] == "GET"
    assert body["error"]["context"]["path"] == "/settle"

    status, body, _ = req(harness, "POST", "/stats")
    assert_envelope(status, body, "STL-405")
    assert body["error"]["context"]["allowed"] == ["GET"]

    # /healthz is a known path (GET only): POST is a 405 with the allowed list.
    status, body, _ = req(harness, "POST", "/healthz")
    assert_envelope(status, body, "STL-405")
    assert body["error"]["context"]["allowed"] == ["GET"]


# ---------------------------------------------------------------------------
# POST /ingest (manual trigger; stub S6, no real upstream)
# ---------------------------------------------------------------------------

def test_ingest_endpoint_stub_and_failure():
    rows = [{"symbol": "FESX", "account": "MAIN", "net_qty": 5, "avg_price": 52.5}]
    stub = StubIngest(rows=rows)
    h = make_harness(ingest_client=stub)
    try:
        status, out, _ = req(h, "POST", "/ingest")
        assert status == 200 and out["ingested"] is True
        assert out["date"] == FIX_DAY and out["fills_added"] == 1
        # Deterministic fill id, visible in the run and idempotent on re-pull.
        status, rep, _ = req(h, "GET", f"/reports/{FIX_DAY}")
        assert status == 200
        assert rep["run"]["fill_count"] == 1
        status, out2, _ = req(h, "POST", "/ingest")
        assert out2["fills_added"] == 0 and out2["duplicate_fills_ignored"] == 1
        assert stub.pull_successes == 2 and stub.pull_failures == 0
        # Stats embed the ingest counters (cumulative + consecutive streak).
        status, stats, _ = req(h, "GET", "/stats")
        assert stats["ingest"]["pull_successes"] == 2
        assert stats["ingest"]["pull_failures"] == 0
        assert stats["ingest"]["consecutive_failures"] == 0

        stub.fail = True
        status, body, _ = req(h, "POST", "/ingest")
        assert_envelope(status, body, "STL-002", retryable=True,
                        context_keys=("pull_failures",))
        assert body["error"]["context"]["pull_failures"] == 1
        assert stub.consecutive_failures == 1  # readyz is now not_ready
    finally:
        h["httpd"].shutdown()
        h["httpd"].server_close()


def test_ingest_endpoint_without_client_is_503(harness):
    status, body, _ = req(harness, "POST", "/ingest")
    assert_envelope(status, body, "STL-002", retryable=True)
    assert body["error"]["context"].get("detail")


# ---------------------------------------------------------------------------
# Full HTTP round trip (mirrors the M1 smoke over the wire)
# ---------------------------------------------------------------------------

def test_full_http_round_trip(harness):
    body = {"date": FIX_DAY,
            "fills": [fill("F1", px=50.0, qty=4, fee=1.0),
                      fill("F2", side="SELL", px=52.0, qty=1, fee=1.0)],
            "statement_lines": [stmt("EUREX", "FESX", px=50.5, qty=5)]}
    status, first, _ = req(harness, "POST", "/settle", body=body)
    assert status == 200 and first["fills_added"] == 2
    status, replay, _ = req(harness, "POST", "/settle", body=body)
    assert replay["duplicate_fills_ignored"] == 2
    status, body409, _ = req(harness, "POST", "/settle",
                             body={"date": FIX_DAY,
                                   "fills": [fill("F1", px=99.0)]})
    assert_envelope(status, body409, "STL-205")
    status, seal, _ = req(harness, "POST", f"/finalize/{FIX_DAY}")
    assert status == 200 and seal["idempotent"] is False
    status, body409, _ = req(harness, "POST", "/settle",
                             body={"date": FIX_DAY, "fills": [fill("Z1")]})
    assert_envelope(status, body409, "STL-206")
    status, rep, _ = req(harness, "GET", f"/reports/{FIX_DAY}")
    assert rep["verify_hash"] == seal["content_hash"]
    assert [d["kind"] for d in rep["discrepancies"]] == ["QTY_MISMATCH"]
    status, run, _ = req(harness, "GET", f"/runs/{FIX_DAY}")
    assert run["run"]["finalized"] is True


def test_load_config_positions_url_flag():
    """``--positions-url`` (Session M, item 2): override of the S6 ingest URL.

    The default ``http://127.0.0.1:7660`` is correct for host-mode runs but
    wrong inside the compose network, where S6 is the ``position-keeper``
    container; the CLI flag must override exactly that field (and only that
    field) while keeping every other default intact.
    """
    import argparse
    from stls.errors import STLError
    from stls.main import load_config

    def ns(**kw):
        base = {"bind": None, "port": None, "positions_url": None}
        base.update(kw)
        return argparse.Namespace(**base)

    # 1. Explicit override lands on ingest.position_keeper_url, nothing else moves.
    cfg = load_config(ns(positions_url="http://position-keeper:7660"))
    assert cfg.ingest.position_keeper_url == "http://position-keeper:7660"
    assert cfg.ingest.interval_ms == 15000 and cfg.ingest.enabled is True
    assert cfg.server.host == "0.0.0.0" and cfg.server.port == 7740

    # 2. Absent flag keeps the built-in default (host-mode behaviour unchanged).
    cfg_default = load_config(ns())
    assert cfg_default.ingest.position_keeper_url == "http://127.0.0.1:7660"

    # 3. Composes with --bind/--port (no cross-interference between the
    #    server namespace and the ingest namespace).
    cfg_both = load_config(ns(bind="127.0.0.1", port=8123,
                              positions_url="http://stub-s6:9999"))
    assert cfg_both.server.host == "127.0.0.1" and cfg_both.server.port == 8123
    assert cfg_both.ingest.position_keeper_url == "http://stub-s6:9999"

    # 4. A non-URL value is rejected at boot by validate_config (STL-101),
    #    mirroring how the other S6/venue URLs are guarded.
    with pytest.raises(STLError) as exc:
        load_config(ns(positions_url="ftp://not-an-http-url"))
    assert exc.value.code == "STL-101"
