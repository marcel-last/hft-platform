"""S14 Milestone 2 — settlement engine unit tests.

Deterministic by construction: every time-dependent path runs through a
:class:`stls.models.ManualClock` (no sleeping, no wall clock).  The fixture
timestamp for 2026-09-10 is derived from a UTC noon via ``timestamp()*1e9``
and the clock's own ``today_date()`` is asserted in the fixture itself
(STATE.md M1 caveat: ``today_date()`` uses local-time ``fromtimestamp``).
"""

from __future__ import annotations

import datetime as dt

import pytest

from stls.config import (
    IngestConfig,
    MatchingConfig,
    NettingConfig,
    RetentionConfig,
    SettlementConfig,
)
from stls.errors import (
    FillIdConflict,
    InvalidDateParam,
    InvalidLimitParam,
    InvalidSettleRequest,
    RunFinalized,
    UnknownRun,
    error_envelope,
)
from stls.models import (
    FillRecord,
    ManualClock,
    StatementLine,
    content_hash,
    parse_fill,
    parse_settlement_date,
    parse_statement_line,
)
from stls.netting import compute_cash_summary, compute_net_positions
from stls.reconcile import aggregate_fills, aggregate_statement, matching_report
from stls.settlement_engine import SettlementEngine

#: Fixed fixture day: UTC noon keeps local-time date math on the same day.
FIX_DAY = "2026-09-10"
FIX_NS = int(dt.datetime(2026, 9, 10, 12, tzinfo=dt.timezone.utc).timestamp() * 1e9)
STEP_MS = 1000


def make_cfg(**overrides) -> SettlementConfig:
    """A default config with per-namespace overrides (dataclasses.replace, frozen)."""
    import dataclasses
    base = SettlementConfig()
    for name, value in overrides.items():
        if not isinstance(value, dict):
            raise AssertionError(f"unknown config namespace {name!r}")
        base = dataclasses.replace(base, **{name: dataclasses.replace(getattr(base, name), **value)})
    return base


def make_engine(**overrides) -> SettlementEngine:
    cfg = make_cfg(**overrides)
    clock = ManualClock(FIX_NS)
    return SettlementEngine(cfg, clock=clock)


def fill(fill_id="F1", venue="EUREX", symbol="FESX", side="BUY", px=50.0,
         qty=1, fee=0.0, account="MAIN", ts_ns=FIX_NS) -> dict:
    """A raw /settle fill wire object (dict), not a FillRecord."""
    return {
        "fill_id": fill_id, "ts_ns": ts_ns, "venue": venue, "symbol": symbol,
        "side": side, "px": px, "qty": qty, "fee": fee, "account": account,
    }


def stmt(venue="EUREX", symbol="FESX", side="BUY", px=50.0, qty=1, fee=0.0,
         stmt_line_id=None, ts_ns=None) -> dict:
    """A raw statement-line wire object."""
    return {
        "venue": venue, "symbol": symbol, "side": side, "px": px, "qty": qty,
        "fee": fee, "stmt_line_id": stmt_line_id, "ts_ns": ts_ns,
    }


def settle(engine, date=FIX_DAY, fills=None, lines=None) -> dict:
    return engine.settle({
        "date": date,
        "fills": [] if fills is None else fills,
        "statement_lines": [] if lines is None else lines,
    })


def advance(engine, ms=STEP_MS) -> None:
    engine.clock.advance_ns(ms * 1_000_000)


def kinds(discrepancies) -> list:
    return [d.kind for d in discrepancies]


def by_kind(discrepancies, kind) -> list:
    return [d for d in discrepancies if d.kind == kind]


# ---------------------------------------------------------------------------
# Fixture sanity: the ManualClock's own date view must match FIX_DAY.
# ---------------------------------------------------------------------------

def test_fixture_clock_date():
    clock = ManualClock(FIX_NS)
    assert clock.today_date() == FIX_DAY
    # Crossing UTC midnight backwards lands on the previous day.
    clock.set(FIX_NS - 36 * 3_600 * 10**9)
    assert clock.today_date() == "2026-09-09"


# ---------------------------------------------------------------------------
# Date validation (STL-202)
# ---------------------------------------------------------------------------

def test_date_valid_past_and_today():
    clock = ManualClock(FIX_NS)
    assert parse_settlement_date("2026-09-10", clock) == "2026-09-10"
    assert parse_settlement_date("2020-01-01", clock) == "2020-01-01"
    assert parse_settlement_date(" 2026-09-09 ", clock) == "2026-09-09"  # stripped
    assert parse_settlement_date("1970-01-01", clock) == "1970-01-01"     # floor ok


@pytest.mark.parametrize("raw", ["2026-09-11", "2999-12-31"])
def test_date_future_rejected(raw):
    clock = ManualClock(FIX_NS)
    with pytest.raises(InvalidDateParam) as exc:
        parse_settlement_date(raw, clock)
    assert exc.value.code == "STL-202"
    assert exc.value.http_status == 400
    assert exc.value.context["raw"] == raw
    assert exc.value.context["today"] == FIX_DAY
    assert exc.value.context["field"] == "date"


def test_date_pre_1970_rejected():
    with pytest.raises(InvalidDateParam) as exc:
        parse_settlement_date("1969-12-31", ManualClock(FIX_NS))
    assert exc.value.code == "STL-202"
    assert exc.value.context["raw"] == "1969-12-31"


@pytest.mark.parametrize("raw", [
    "garbage", "2026-9-1", "2026/09/10", "2026-02-30", "2026-09-10T00:00:00",
    "", "   ", "2026-09-1",
])
def test_date_garbage_rejected(raw):
    with pytest.raises(InvalidDateParam) as exc:
        parse_settlement_date(raw, ManualClock(FIX_NS))
    assert exc.value.code == "STL-202"
    assert exc.value.context["field"] == "date"


@pytest.mark.parametrize("raw", [20260910, None, 2026.5, True, ["2026-09-10"]])
def test_date_non_string_rejected(raw):
    with pytest.raises(InvalidDateParam) as exc:
        parse_settlement_date(raw, ManualClock(FIX_NS))
    assert exc.value.code == "STL-202"
    assert exc.value.context["raw"] == repr(raw)


def test_date_missing_rejected():
    eng = make_engine()
    with pytest.raises(InvalidDateParam):
        eng.settle({"fills": [], "statement_lines": []})
    with pytest.raises(InvalidDateParam):
        eng.settle({"date": None})
    # The date guard fires before any run state exists.
    assert eng.list_dates() == []


# ---------------------------------------------------------------------------
# Fill parsing (STL-201 field guards)
# ---------------------------------------------------------------------------

def test_fill_parse_happy_normalization():
    f = parse_fill(fill(fill_id="f1", venue="eurex", symbol="fesx", side="buy",
                        px=50, fee=0, account="main"), 0)
    assert isinstance(f, FillRecord)
    assert (f.fill_id, f.venue, f.symbol, f.side, f.account) == (
        "f1", "EUREX", "FESX", "BUY", "MAIN")
    assert f.ts_ns == FIX_NS
    assert f.px == 50.0 and f.qty == 1 and f.fee == 0.0
    assert f.notional == 50.0


def test_fill_defaults_side_fee_account():
    f = parse_fill({"fill_id": "F1", "ts_ns": 5, "venue": "V", "symbol": "S",
                    "px": 1.0, "qty": 1}, 0)
    assert (f.side, f.fee, f.account) == ("BUY", 0.0, "MAIN")


def test_fill_rejects_non_object():
    with pytest.raises(InvalidSettleRequest) as exc:
        parse_fill(["not", "a", "dict"], 2)
    assert exc.value.code == "STL-201"
    assert exc.value.http_status == 400
    assert exc.value.context["field"] == "fills[2]"


@pytest.mark.parametrize("field, value", [
    ("fill_id", ""),
    ("fill_id", "x" * 129),
    ("fill_id", None),
    ("ts_ns", -1),
    ("ts_ns", 1.5),
    ("ts_ns", True),
    ("venue", ""),
    ("venue", "v" * 65),
    ("symbol", ""),
    ("symbol", "s" * 65),
    ("side", "HOLD"),
    ("side", 3),
    ("px", -0.01),
    ("px", "50"),
    ("px", True),
    ("qty", 0),
    ("qty", -3),
    ("qty", 2.5),
    ("qty", True),
    ("fee", -0.1),
    ("fee", "1"),
    ("account", "" * 65),
])
def test_fill_field_guards(field, value):
    payload = fill()
    payload[field] = value
    with pytest.raises(InvalidSettleRequest) as exc:
        parse_fill(payload, 0)
    assert exc.value.code == "STL-201"
    assert exc.value.context["field"] == "fills[0]"


def test_fill_length_caps_at_limit_pass():
    assert parse_fill(fill(fill_id="y" * 128, venue="v" * 64, symbol="s" * 64,
                           account="a" * 64), 0).fill_id == "y" * 128


# ---------------------------------------------------------------------------
# Fill idempotency (per-run digests)
# ---------------------------------------------------------------------------

def test_fill_idempotency_identical_replay_is_counted_noop():
    eng = make_engine()
    first = settle(eng, fills=[fill("F1"), fill("F2")])
    assert first["fills_added"] == 2
    assert first["duplicate_fills_ignored"] == 0

    replay = settle(eng, fills=[fill("F1"), fill("F2")])
    assert replay["fills_added"] == 0
    assert replay["duplicate_fills_ignored"] == 2
    assert replay["run"]["fill_count"] == 2
    assert replay["run"]["duplicate_fills_ignored"] == 2
    assert replay["run"]["settle_calls"] == 2


def test_fill_idempotency_mixed_new_and_duplicate():
    eng = make_engine()
    settle(eng, fills=[fill("F1")])
    out = settle(eng, fills=[fill("F1"), fill("F3", symbol="ES")])
    assert out["fills_added"] == 1
    assert out["duplicate_fills_ignored"] == 1
    assert out["run"]["fill_count"] == 2


def test_fill_idempotency_same_id_different_payload_conflicts():
    eng = make_engine()
    settle(eng, fills=[fill("F1", px=50.0)])
    with pytest.raises(FillIdConflict) as exc:
        settle(eng, fills=[fill("F1", px=51.0)])
    assert exc.value.code == "STL-205"
    assert exc.value.http_status == 409
    assert exc.value.retryable is False
    assert exc.value.context == {"fill_id": "F1", "date": FIX_DAY}
    env = error_envelope(exc.value)
    assert env["error"]["service"] == "settlement-service"
    assert env["error"]["retryable"] is False


def test_fill_conflict_rejected_without_partial_mutation():
    eng = make_engine()
    settle(eng, fills=[fill("F1")])
    with pytest.raises(FillIdConflict):
        settle(eng, fills=[fill("F1", px=51.0), fill("F9")])
    # The whole body is pre-parsed and the conflict raises before any append.
    assert eng.list_dates() == [FIX_DAY]
    assert eng.report(FIX_DAY)["run"]["fill_count"] == 1
    assert eng.stats()["fill_id_conflicts"] == 1


def test_fill_digest_scoped_per_run_date():
    eng = make_engine()
    settle(eng, fills=[fill("F1")])
    # Same fill id on a different date is a different run: no conflict.
    out = settle(eng, date="2026-09-09", fills=[fill("F1", px=99.0)])
    assert out["fills_added"] == 1
    assert out["run"]["fill_count"] == 1
    # And the original date is untouched by the second run.
    assert eng.report(FIX_DAY)["run"]["fill_count"] == 1
    assert eng.report("2026-09-09")["run"]["fill_count"] == 1


# ---------------------------------------------------------------------------
# Per-venue statement replacement
# ---------------------------------------------------------------------------

def test_statement_replacement_untouched_venues_keep_lines():
    eng = make_engine()
    settle(eng, lines=[stmt("CME", "ES"), stmt("EUREX", "FESX"),
                       stmt("EUREX", "FESX", side="SELL")])
    assert eng.report(FIX_DAY)["run"]["statement_line_count"] == 3

    # Re-issue EUREX's statement only: its lines are replaced, CME's survive.
    out = settle(eng, lines=[stmt("EUREX", "FESX", qty=7)])
    assert out["statement_venues_replaced"] == ["EUREX"]
    rep = eng.report(FIX_DAY)
    lines = rep["run"]
    assert lines["statement_line_count"] == 2
    # Re-read through the reconcile layer to prove the surviving lines.
    disc = eng.discrepancies(None, None, None, None)
    assert {d["venue"] for d in disc["discrepancies"]} == {"CME", "EUREX"}


def test_statement_replacement_replaces_all_venues_in_body():
    eng = make_engine()
    settle(eng, lines=[stmt("CME", "ES"), stmt("NYAR", "NQ")])
    out = settle(eng, lines=[stmt("CME", "ES", qty=4), stmt("NYAR", "NQ", qty=5)])
    assert out["statement_venues_replaced"] == ["CME", "NYAR"]
    assert eng.report(FIX_DAY)["run"]["statement_line_count"] == 2
    rep = eng.report(FIX_DAY)
    # No fills exist in this run: both replaced statement keys are unexpected.
    assert [d["kind"] for d in rep["discrepancies"]] == [
        "UNEXPECTED_SYMBOL", "UNEXPECTED_SYMBOL"]
    assert rep["discrepancy_count"] == 2


def test_statement_replacement_no_lines_is_a_noop_for_statements():
    eng = make_engine()
    settle(eng, lines=[stmt("CME", "ES")])
    out = settle(eng, fills=[fill("F1")])
    assert out["statement_venues_replaced"] == []
    assert eng.report(FIX_DAY)["run"]["statement_line_count"] == 1


def test_statement_per_venue_submission_cap():
    eng = make_engine(matching={"max_statement_lines_per_venue": 3})
    with pytest.raises(InvalidSettleRequest) as exc:
        settle(eng, lines=[stmt("CME", "ES") for _ in range(4)])
    assert exc.value.code == "STL-201"
    assert exc.value.context["venue"] == "CME"
    assert exc.value.context["count"] == 4
    assert exc.value.context["cap"] == 3


def test_statement_ids_auto_assigned_and_nullable_ts():
    lines = [parse_statement_line(stmt("CME", "ES"), 0, seq=0),
             parse_statement_line(stmt("CME", "ES"), 1, seq=1),
             parse_statement_line(stmt("CME", "ES", ts_ns=None), 2, seq=2)]
    assert lines[0].stmt_line_id == "STM-CME-0"
    assert lines[1].stmt_line_id == "STM-CME-1"
    assert lines[2].ts_ns is None
    # Explicit ids are kept.
    assert parse_statement_line(stmt(stmt_line_id="CUSTOM"), 0, seq=9).stmt_line_id == "CUSTOM"


# ---------------------------------------------------------------------------
# Reconciliation (matching_report)
# ---------------------------------------------------------------------------

def test_reconcile_qty_mismatch_checked_before_price_mismatch():
    eng = make_engine()
    settle(eng, fills=[fill("F1", px=50.0, qty=10)],
           lines=[stmt("EUREX", "FESX", px=60.0, qty=5)])  # qty 10 vs 5 AND px 50 vs 60
    disc = eng.discrepancies(None, None, None, None)["discrepancies"]
    assert kinds(eng._discrepancies(eng._runs[FIX_DAY])) == ["QTY_MISMATCH"]
    d = disc[0]
    assert d["kind"] == "QTY_MISMATCH"
    assert d["severity"] == "CRITICAL"
    assert d["our_qty"] == 10 and d["stmt_qty"] == 5
    assert d["qty_delta"] == 5


def test_reconcile_qty_within_tolerance_then_price_checked():
    eng = make_engine(matching={"qty_tolerance": 1})
    settle(eng, fills=[fill("F1", px=50.0, qty=10)],
           lines=[stmt("EUREX", "FESX", px=60.0, qty=9)])  # qty ok (1), px far off
    found = eng._discrepancies(eng._runs[FIX_DAY])
    assert kinds(found) == ["PRICE_MISMATCH"]
    assert found[0].severity == "WARNING"
    # And with qty also over tolerance the QTY finding wins (price check is
    # suppressed for that key: the report records at most one finding per key).
    settle(eng, fills=[fill("F2", px=50.0, qty=4)],
           lines=[stmt("EUREX", "FESX", px=60.0, qty=5)])
    found2 = eng._discrepancies(eng._runs[FIX_DAY])
    assert [d.kind for d in found2] == ["QTY_MISMATCH"]


def test_reconcile_qty_tolerance_boundaries():
    eng = make_engine(matching={"qty_tolerance": 2})
    settle(eng, fills=[fill("F1", px=50.0, qty=10)],
           lines=[stmt("EUREX", "FESX", px=50.0, qty=8)])  # delta exactly 2
    assert kinds(eng._discrepancies(eng._runs[FIX_DAY])) == []
    settle(eng, lines=[stmt("EUREX", "FESX", px=50.0, qty=7)])  # delta 3 > tol
    assert kinds(eng._discrepancies(eng._runs[FIX_DAY])) == ["QTY_MISMATCH"]


def test_reconcile_price_tolerance_boundaries():
    eng = make_engine(matching={"price_tolerance_abs": 0.5})
    settle(eng, fills=[fill("F1", px=100.0, qty=1)],
           lines=[stmt("EUREX", "FESX", px=100.5, qty=1)])  # delta exactly 0.5
    assert kinds(eng._discrepancies(eng._runs[FIX_DAY])) == []
    settle(eng, lines=[stmt("EUREX", "FESX", px=100.5001, qty=1)])  # 0.5001 > tol
    assert kinds(eng._discrepancies(eng._runs[FIX_DAY])) == ["PRICE_MISMATCH"]


def test_reconcile_clean_match_no_findings():
    eng = make_engine()
    settle(eng, fills=[fill("F1", px=50.0, qty=3, fee=1.5)],
           lines=[stmt("EUREX", "FESX", px=50.0, qty=3, fee=1.5)])
    assert eng.discrepancies(None, None, None, None) == {
        "discrepancies": [], "count": 0, "truncated": False}


def test_reconcile_missing_statement_and_unexpected_symbol():
    eng = make_engine()
    settle(eng, fills=[fill("F1", venue="CME", symbol="ES", px=5000.0, qty=2)],
           lines=[stmt("EUREX", "FESX", px=10.0, qty=1)])
    found = eng._discrepancies(eng._runs[FIX_DAY])
    assert [(d.kind, d.venue, d.symbol) for d in found] == [
        ("MISSING_STATEMENT", "CME", "ES"),
        ("UNEXPECTED_SYMBOL", "EUREX", "FESX")]
    miss = by_kind(found, "MISSING_STATEMENT")[0]
    assert miss.our_qty == 2 and miss.stmt_qty == 0
    assert miss.severity == "CRITICAL" and miss.detected_ns == eng.clock.now_ns()
    unexp = by_kind(found, "UNEXPECTED_SYMBOL")[0]
    assert unexp.our_qty == 0 and unexp.stmt_qty == 1
    assert unexp.severity == "WARNING"


def test_reconcile_deterministic_ordering_and_detected_ns_tracking():
    eng = make_engine()
    settle(eng,
           fills=[fill("F1", venue="B", symbol="ZZ"),
                  fill("F2", venue="A", symbol="MM")],
           lines=[stmt("C", "QQ"), stmt("A", "MM", qty=9), stmt("B", "ZZ", qty=9)])
    # A/MM: qty 1 vs 9 -> QTY; A/MM px 50 vs 50 ok; B/ZZ: 1 vs 9 -> QTY; C/QQ unexpected.
    found = eng._discrepancies(eng._runs[FIX_DAY])
    assert [(d.venue, d.symbol, d.kind) for d in found] == [
        ("A", "MM", "QTY_MISMATCH"),
        ("B", "ZZ", "QTY_MISMATCH"),
        ("C", "QQ", "UNEXPECTED_SYMBOL")]
    assert all(d.detected_ns == FIX_NS for d in found)
    advance(eng, 2500)
    found2 = eng._discrepancies(eng._runs[FIX_DAY])
    assert all(d.detected_ns == FIX_NS + 2_500_000_000 for d in found2)


def test_reconcile_statement_replacement_clears_stale_findings():
    eng = make_engine()
    settle(eng, fills=[fill("F1", px=50.0, qty=3)],
           lines=[stmt("EUREX", "FESX", px=50.0, qty=9)])
    assert kinds(eng._discrepancies(eng._runs[FIX_DAY])) == ["QTY_MISMATCH"]
    settle(eng, lines=[stmt("EUREX", "FESX", px=50.0, qty=3)])  # venue re-issues, fixed
    assert eng.discrepancies(None, None, None, None)["count"] == 0


def test_aggregates_net_gross_and_weighted_avg():
    fills = [parse_fill(fill("A1", px=40.0, qty=3), 0),
             parse_fill(fill("A2", side="SELL", px=60.0, qty=1), 1)]
    tot = aggregate_fills(fills, "EUREX", "FESX")
    assert (tot.net_qty, tot.gross_qty) == (2, 4)
    assert tot.notional == pytest.approx(60.0)  # +120 (buy) - 60 (sell), signed
    assert tot.absolute_notional == pytest.approx(60.0)
    assert tot.avg_px == pytest.approx(60.0 / 4)  # abs(signed)/gross basis
    assert tot.line_count == 2
    lines = [parse_statement_line(stmt("EUREX", "FESX", px=50.0, qty=2), 0, seq=0)]
    stot = aggregate_statement(lines, "EUREX", "FESX")
    assert (stot.net_qty, stot.avg_px) == (2, 50.0)


# ---------------------------------------------------------------------------
# Netting and cash
# ---------------------------------------------------------------------------

def test_netting_math():
    eng = make_engine()
    settle(eng, fills=[
        fill("F1", symbol="FESX", px=100.0, qty=3, fee=1.0, account="MAIN"),
        fill("F2", symbol="FESX", side="SELL", px=110.0, qty=1, fee=0.5, account="MAIN"),
        fill("F3", symbol="ES", px=5000.0, qty=2, account="ALPHA"),
        fill("F4", symbol="ES", side="SELL", px=5100.0, qty=2, account="ALPHA"),
    ])
    rep = eng.report(FIX_DAY)
    rows = {(p["account"], p["symbol"]): p for p in rep["net_positions"]}
    assert set(rows) == {("MAIN", "FESX"), ("ALPHA", "ES")}

    fesx = rows[("MAIN", "FESX")]
    assert (fesx["buy_qty"], fesx["sell_qty"], fesx["net_qty"]) == (3, 1, 2)
    assert fesx["buy_notional"] == 300.0 and fesx["sell_notional"] == 110.0
    assert fesx["net_cash_flow"] == pytest.approx(-190.0)
    assert fesx["fees"] == pytest.approx(1.5)
    assert fesx["fill_count"] == 2
    assert fesx["avg_fill_px"] == pytest.approx(410.0 / 4)  # weighted over turnover

    es = rows[("ALPHA", "ES")]
    assert es["net_qty"] == 0
    assert es["net_cash_flow"] == pytest.approx(200.0)  # flat qty but net cash != 0
    assert es["avg_fill_px"] == pytest.approx(20200.0 / 4)  # turnover-weighted

    cash = rep["cash_summary"]
    assert cash["buy_notional"] == 10300.0 and cash["sell_notional"] == 10310.0
    assert cash["total_fees"] == pytest.approx(1.5)
    assert cash["net_cash"] == pytest.approx(10310.0 - 10300.0 - 1.5)  # 8.5
    assert cash["fill_count"] == 4


def test_netting_flat_row_dropped_and_flag_toggles():
    fills = [fill("F1", px=100.0, qty=1), fill("F2", side="SELL", px=100.0, qty=1)]
    cfg = SettlementConfig()
    rows_on = compute_net_positions([parse_fill(f, i) for i, f in enumerate(fills)], cfg)
    assert rows_on == []  # zero_out_flat_positions=True (default)

    cfg_off = SettlementConfig(netting=NettingConfig(zero_out_flat_positions=False))
    rows_off = compute_net_positions([parse_fill(f, i) for i, f in enumerate(fills)], cfg_off)
    assert len(rows_off) == 1
    assert rows_off[0].net_qty == 0 and rows_off[0].net_cash_flow == pytest.approx(0.0)

    # Flat qty but net notional != 0 survives even with zero-out on.
    skew = [fill("F1", px=100.0, qty=1), fill("F2", side="SELL", px=200.0, qty=1)]
    rows2 = compute_net_positions([parse_fill(f, i) for i, f in enumerate(skew)], cfg)
    assert len(rows2) == 1 and rows2[0].net_cash_flow == pytest.approx(100.0)


def test_netting_row_cap_and_cash_summary():
    eng = make_engine(netting={"max_net_positions_per_run": 2})
    fills = [fill(f"F{i}", symbol=f"S{i:02d}", account="MAIN") for i in range(5)]
    settle(eng, fills=fills)
    rep = eng.report(FIX_DAY)
    assert len(rep["net_positions"]) == 2
    # Cap keeps the first rows after deterministic (account, symbol) sort.
    assert [p["symbol"] for p in rep["net_positions"]] == ["S00", "S01"]
    cash = rep["cash_summary"]
    assert cash["fill_count"] == 5 and cash["net_cash"] == pytest.approx(-250.0)
    assert cash["buy_notional"] == 250.0 and cash["sell_notional"] == 0.0


# ---------------------------------------------------------------------------
# Finalization: sealing, stability, idempotency, freeze
# ---------------------------------------------------------------------------

def test_finalize_seals_and_is_idempotent():
    eng = make_engine()
    settle(eng, fills=[fill("F1", px=50.0, qty=2, fee=0.25)],
           lines=[stmt("EUREX", "FESX", px=50.0, qty=2)])
    advance(eng)
    first = eng.finalize_eod(FIX_DAY)
    assert first == {
        "date": FIX_DAY, "finalized": True, "idempotent": False,
        "content_hash": first["content_hash"], "run": first["run"]}
    assert first["run"]["kind"] == "eod" and first["run"]["finalized"] is True
    assert isinstance(first["content_hash"], str) and len(first["content_hash"]) == 64

    advance(eng)
    again = eng.finalize_eod(FIX_DAY)
    assert again["idempotent"] is True
    assert again["content_hash"] == first["content_hash"]  # re-seal = same seal
    assert eng.stats()["finalized_runs"] == 1


def test_finalize_verify_hash_stable_and_independent():
    eng = make_engine()
    settle(eng, fills=[fill("F1", px=50.0, qty=2, fee=0.25)],
           lines=[stmt("EUREX", "FESX", px=50.0, qty=2)])
    seal = eng.finalize_eod(FIX_DAY)
    rep = eng.report(FIX_DAY)
    assert rep["verify_hash"] is not None
    assert rep["verify_hash"] == seal["content_hash"]

    # Independent recomputation from the run's wire state (tamper-evidence).
    run = eng._runs[FIX_DAY]
    snapshot = {
        "date": run.date,
        "fills": [f.__dict__ for f in run.fills],
        "statement_lines": [l.__dict__ for l in run.statement_lines],
        "net_positions": [p.to_dict() for p in compute_net_positions(run.fills, eng.cfg)],
        "cash_summary": compute_cash_summary(run.fills).to_dict(),
    }
    assert content_hash(snapshot) == seal["content_hash"]
    # Stable across repeated reads, even with the clock advanced.
    advance(eng, 5000)
    assert eng.report(FIX_DAY)["verify_hash"] == seal["content_hash"]


def test_finalize_verify_hash_none_before_seal():
    eng = make_engine()
    settle(eng, fills=[fill("F1")])
    assert eng.report(FIX_DAY)["verify_hash"] is None
    assert eng.report(FIX_DAY)["run"]["content_hash"] is None


def test_finalize_unknown_and_invalid_dates():
    eng = make_engine()
    with pytest.raises(UnknownRun) as exc:
        eng.finalize_eod("2026-09-01")
    assert exc.value.code == "STL-204" and exc.value.http_status == 404
    assert exc.value.context["date"] == "2026-09-01"
    assert exc.value.context["known_dates"] == []
    with pytest.raises(InvalidDateParam):
        eng.finalize_eod("nope")
    with pytest.raises(InvalidDateParam):
        eng.finalize_eod("2030-01-01")


def test_settle_on_finalized_run_rejected_stl206():
    eng = make_engine()
    settle(eng, fills=[fill("F1")])
    eng.finalize_eod(FIX_DAY)
    with pytest.raises(RunFinalized) as exc:
        settle(eng, fills=[fill("F2")])
    assert exc.value.code == "STL-206" and exc.value.http_status == 409
    assert exc.value.context["date"] == FIX_DAY
    assert exc.value.context["content_hash"] == eng._runs[FIX_DAY].content_hash
    # Statement replacement is frozen too.
    with pytest.raises(RunFinalized):
        settle(eng, lines=[stmt("EUREX", "FESX", qty=5)])
    assert eng.report(FIX_DAY)["run"]["fill_count"] == 1


# ---------------------------------------------------------------------------
# Revisions (bounded history)
# ---------------------------------------------------------------------------

def test_revisions_recorded_and_bounded():
    eng = make_engine(retention={"max_runs_per_date_history": 3})
    settle(eng, fills=[fill("F1")])
    settle(eng, fills=[fill("F1")])   # duplicate-fold replay -> still a revision
    settle(eng, fills=[fill("F2")])
    advance(eng)
    settle(eng, fills=[fill("F3")])
    settle(eng, fills=[fill("F4")])
    eng.finalize_eod(FIX_DAY)

    # Six revisions recorded (5 settle + 1 finalize), bounded to the latest 3.
    revs = eng.report(FIX_DAY)["revisions"]
    assert len(revs) == 3  # bounded to max_runs_per_date_history
    # seq is a per-date lifetime monotonic counter: trimming the window never
    # renumbers the survivors (a re-numbering would break audit references).
    assert [r["seq"] for r in revs] == [4, 5, 6]
    assert [r["action"] for r in revs] == ["settle", "settle", "finalize"]
    # The oldest surviving entry is settle #4 (F3, one fill added, no dups).
    assert revs[0]["fills_added"] == 1 and revs[0]["fills_duplicated"] == 0
    # Newest entry is the finalization, stamped at the current clock.
    assert revs[-1]["action"] == "finalize" and revs[-1]["ts_ns"] == eng.clock.now_ns()

    stats = eng.stats()
    assert stats["settle_calls"] == 5
    assert stats["duplicate_fills_ignored"] == 1


def test_revisions_captured_even_when_conflict_raises():
    eng = make_engine(retention={"max_runs_per_date_history": 3})
    settle(eng, fills=[fill("F1")])
    with pytest.raises(FillIdConflict):
        settle(eng, fills=[fill("F1", px=99.0)])
    # The conflicted settle is rejected before it mutates the run: it leaves
    # no revision and is not counted as a settle call (bad bodies never mutate).
    revs = eng.report(FIX_DAY)["revisions"]
    assert len(revs) == 1
    assert revs[0]["action"] == "settle" and revs[0]["fills_added"] == 1
    s = eng.stats()
    assert s["fill_id_conflicts"] == 1 and s["settle_calls"] == 1


# ---------------------------------------------------------------------------
# Unknown-run reads (STL-204)
# ---------------------------------------------------------------------------

def test_unknown_run_reads():
    eng = make_engine()
    for call in (lambda: eng.report("2020-01-01"),
                 lambda: eng.finalize_eod("2020-01-01")):
        with pytest.raises(UnknownRun) as exc:
            call()
        assert exc.value.code == "STL-204" and exc.value.http_status == 404
    # A bad limit on /discrepancies is STL-203 even with no runs at all.
    with pytest.raises(InvalidLimitParam) as exc:
        eng.discrepancies(None, None, None, "zero")
    assert exc.value.code == "STL-203" and exc.value.http_status == 400
    with pytest.raises(InvalidLimitParam):
        eng.discrepancies(None, None, None, "0")


# ---------------------------------------------------------------------------
# S6 position ingest
# ---------------------------------------------------------------------------

POSITIONS = [
    {"symbol": "FESX", "account": "MAIN", "net_qty": 5, "avg_price": 52.5},
    {"symbol": "ES", "account": "ALPHA", "net_qty": -2, "avg_price": 5100.0,
     "venue": "cme"},
    {"symbol": "NQ", "account": "MAIN", "net_qty": 0, "avg_price": 20000.0},
    {"symbol": "CL", "account": "BETA"},  # net_qty missing -> flat, skipped
]


def test_ingest_deterministic_fills_and_sides():
    eng = make_engine()
    out = eng.ingest_from_positions(POSITIONS)
    assert out["date"] == FIX_DAY
    assert out["fills_added"] == 2
    assert out["duplicate_fills_ignored"] == 0
    rep = eng.report(FIX_DAY)
    assert rep["run"]["fill_count"] == 2
    ids = {f["fill_id"] for f in _fills_dict(eng)}
    assert ids == {f"ING-{FIX_DAY}-MAIN-FESX", f"ING-{FIX_DAY}-ALPHA-ES"}
    fillmap = {f["fill_id"]: f for f in _fills_dict(eng)}
    fesx = fillmap[f"ING-{FIX_DAY}-MAIN-FESX"]
    assert (fesx["side"], fesx["qty"], fesx["px"], fesx["fee"]) == ("BUY", 5, 52.5, 0.0)
    assert fesx["venue"] == "INTERNAL" and fesx["account"] == "MAIN"
    es = fillmap[f"ING-{FIX_DAY}-ALPHA-ES"]
    assert (es["side"], es["qty"], es["px"], es["venue"]) == ("SELL", 2, 5100.0, "CME")
    # Flat positions (net_qty == 0 or absent) contribute nothing.
    assert rep["net_positions"] and all(
        p["symbol"] in ("FESX", "ES") for p in rep["net_positions"])


def test_ingest_idempotent_repull():
    eng = make_engine()
    eng.ingest_from_positions(POSITIONS)
    advance(eng)  # later pull, same snapshot
    out2 = eng.ingest_from_positions(POSITIONS)
    assert out2["fills_added"] == 0
    assert out2["duplicate_fills_ignored"] == 2
    assert eng.report(FIX_DAY)["run"]["fill_count"] == 2
    assert eng.stats()["duplicate_fills_ignored"] == 2


def test_ingest_flat_positions_only_is_noop():
    eng = make_engine()
    out = eng.ingest_from_positions([
        {"symbol": "NQ", "net_qty": 0, "avg_price": 20000.0},
    ])
    assert out == {"settled_fills": 0, "date": FIX_DAY}
    assert eng.list_dates() == []  # no run was created at all


def test_ingest_rejects_invalid_payloads():
    eng = make_engine()
    with pytest.raises(InvalidSettleRequest) as exc:
        eng.ingest_from_positions("nope")
    assert exc.value.code == "STL-201"
    assert exc.value.context["field"] == "positions"
    with pytest.raises(InvalidSettleRequest) as exc:
        eng.ingest_from_positions(["row"])  # non-dict row
    assert exc.value.context["field"] == "positions[0]"
    with pytest.raises(InvalidSettleRequest) as exc:
        eng.ingest_from_positions([{"symbol": 7, "net_qty": 3}])
    assert exc.value.context["field"] == "positions[0].symbol"
    # A bool net_qty is neither a valid int nor zero-skippable: it is skipped
    # (contributes no fill) rather than raising, so the pull is a no-op.
    out_bool = eng.ingest_from_positions([{"symbol": "FESX", "net_qty": True,
                                           "avg_price": 1.0}])
    assert out_bool == {"settled_fills": 0, "date": FIX_DAY}
    assert eng.list_dates() == []
    with pytest.raises(InvalidSettleRequest) as exc:
        eng.ingest_from_positions([{"symbol": "FESX", "net_qty": 3, "avg_price": -1}])
    assert exc.value.context["field"] == "positions[0].avg_price"


def _fills_dict(eng) -> list:
    return [f.__dict__ for f in eng._runs[FIX_DAY].fills]


# ---------------------------------------------------------------------------
# Stats consistency
# ---------------------------------------------------------------------------

def test_stats_counters_consistent():
    eng = make_engine()
    s0 = eng.stats()
    assert s0 == {
        "run_count": 0, "finalized_runs": 0, "settle_calls": 0,
        "duplicate_fills_ignored": 0, "fill_id_conflicts": 0,
        "finalization_conflicts": 0, "dates": [], "runs": {}}

    settle(eng, fills=[fill("F1"), fill("F2")])
    settle(eng, fills=[fill("F1"), fill("F3", symbol="ES")])
    with pytest.raises(FillIdConflict):
        settle(eng, fills=[fill("F1", px=77.0)])
    settle(eng, date="2026-09-09", fills=[fill("F9")])
    advance(eng)
    eng.finalize_eod(FIX_DAY)

    s = eng.stats()
    assert s["run_count"] == 2
    assert s["dates"] == ["2026-09-09", FIX_DAY]
    assert s["settle_calls"] == 3  # the conflicted call is not counted
    assert s["duplicate_fills_ignored"] == 1
    assert s["fill_id_conflicts"] == 1
    assert s["finalization_conflicts"] == 0
    assert s["finalized_runs"] == 1
    day = s["runs"][FIX_DAY]
    assert day["kind"] == "eod" and day["finalized"] is True
    assert day["fill_count"] == 3 and day["settle_calls"] == 2  # conflict not counted
    assert day["duplicate_fills_ignored"] == 1
    assert s["runs"]["2026-09-09"]["kind"] == "intraday"
    assert s["runs"]["2026-09-09"]["fill_count"] == 1  # only F9 settled that day


def test_stats_dates_bounded_by_max_report_dates():
    eng = make_engine(retention={"max_report_dates": 2})
    for d in ("2026-09-07", "2026-09-08", "2026-09-09"):
        settle(eng, date=d, fills=[fill(f"F-{d}")])
    s = eng.stats()
    assert s["run_count"] == 3  # true total, even when the list is bounded
    assert s["dates"] == ["2026-09-08", "2026-09-09"]  # bounded, newest kept
    assert set(s["runs"]) == {"2026-09-08", "2026-09-09"}


# ---------------------------------------------------------------------------
# End-to-end engine round trip (M1 smoke, as unit tests)
# ---------------------------------------------------------------------------

def test_full_engine_round_trip():
    eng = make_engine()
    # Two venues, three fills; statement covers only EUREX.
    settle(eng, fills=[
        fill("F1", venue="CME", symbol="ES", px=5000.0, qty=3, fee=2.0),
        fill("F2", venue="EUREX", symbol="FESX", px=50.0, qty=4, fee=1.0),
        fill("F3", venue="EUREX", symbol="FESX", side="SELL", px=52.0, qty=1, fee=1.0),
    ], lines=[stmt("EUREX", "FESX", px=50.5, qty=5)])
    # Idempotent replay of everything sent so far.
    out = settle(eng, fills=[
        fill("F1", venue="CME", symbol="ES", px=5000.0, qty=3, fee=2.0),
        fill("F2", venue="EUREX", symbol="FESX", px=50.0, qty=4, fee=1.0),
        fill("F3", venue="EUREX", symbol="FESX", side="SELL", px=52.0, qty=1, fee=1.0),
    ], lines=[stmt("EUREX", "FESX", px=50.5, qty=5)])
    assert out["fills_added"] == 0 and out["duplicate_fills_ignored"] == 3

    # Conflicting reuse of F1 is a 409.
    with pytest.raises(FillIdConflict):
        settle(eng, fills=[fill("F1", venue="CME", symbol="ES", px=5001.0, qty=3)])

    # S6 ingest folds into the same run deterministically.
    ing = eng.ingest_from_positions(POSITIONS)
    assert ing["fills_added"] == 2

    rep = eng.report(FIX_DAY)
    assert rep["run"]["fill_count"] == 5
    # Sorted by (venue, symbol, kind): CME/ES missing, EUREX/FESX qty 3 vs 5,
    # INTERNAL/FESX (ingest) missing.
    assert [d["kind"] for d in rep["discrepancies"]] == [
        "MISSING_STATEMENT", "QTY_MISMATCH", "MISSING_STATEMENT"]
    assert rep["cash_summary"]["fill_count"] == 5
    # Netting: FESX MAIN local +4-1=+3, ingest +5 -> +8; ES splits by account.
    by_acct_sym = {(p["account"], p["symbol"]): p for p in rep["net_positions"]}
    assert by_acct_sym[("MAIN", "FESX")]["net_qty"] == 8
    assert by_acct_sym[("MAIN", "ES")]["net_qty"] == 3
    assert by_acct_sym[("ALPHA", "ES")]["net_qty"] == -2
    assert len(rep["net_positions"]) == 3

    # Seal, re-seal, freeze.
    advance(eng)
    seal = eng.finalize_eod(FIX_DAY)
    assert eng.report(FIX_DAY)["verify_hash"] == seal["content_hash"]
    again = eng.finalize_eod(FIX_DAY)
    assert again["idempotent"] is True and again["content_hash"] == seal["content_hash"]
    with pytest.raises(RunFinalized):
        settle(eng, fills=[fill("F9")])
    with pytest.raises(UnknownRun):
        eng.report("2020-01-01")
    with pytest.raises(InvalidDateParam):
        eng.report("2031-01-01")
