"""portfolio_analytics — unit tests (stdlib + pytest-compatible, no network).

Exercises config validation, model parsing, the pure numeric helpers (Sharpe,
drawdown, win rate, VaR/CVaR historical + parametric, normal quantiles), the
engine's P&L / attribution / metrics / history views, error envelopes, and the
router.  All upstream data is injected directly into the engine (no HTTP).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Make the src/ package importable when running tests from the service dir.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pfa.config import (  # noqa: E402
    HistoryConfig,
    IngestConfig,
    MetricsConfig,
    RiskConfig,
    ServiceConfig,
    validate_config,
)
from pfa.errors import (  # noqa: E402
    PFAError,
    UnknownSymbolError,
    UpstreamUnreachableError,
    InsufficientDataError,
    error_envelope,
)
from pfa.models import (  # noqa: E402
    AttributionRow,
    EquitySample,
    PnlRow,
    PortfolioMetrics,
    PositionRow,
    VarResult,
    now_ns,
)
from pfa.analytics_engine import (  # noqa: E402
    AnalyticsEngine,
    _mean,
    _stdev_population,
    _sharpe,
    _max_drawdown_pct,
    _current_drawdown_pct,
    _win_rate_pct,
    _annualized_return,
    _historical_var,
    _parametric_var,
    _norm_ppf,
    _norm_cdf,
)
from pfa.router import build_router  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_engine(cfg: Optional[ServiceConfig] = None,
                **cfg_kw) -> AnalyticsEngine:
    """Build an engine. Pass a full ``cfg`` or individual sub-config kwargs."""
    if cfg is not None:
        return AnalyticsEngine(cfg)
    return AnalyticsEngine(ServiceConfig(**cfg_kw))


def pos_row(symbol: str, net_qty: int = 0, avg_price: float = 0.0,
            realized: float = 0.0, last_fill: float = 0.0,
            ref_px=None, market_value: float = 0.0, unrealized: float = 0.0,
            account: str = "MAIN") -> dict:
    """Build an S6 /positions-shaped row."""
    side = "LONG" if net_qty > 0 else ("SHORT" if net_qty < 0 else "FLAT")
    return {
        "symbol": symbol,
        "account": account,
        "net_qty": net_qty,
        "side": side,
        "avg_price": avg_price,
        "gross_cost": abs(net_qty) * avg_price,
        "realized_pnl": realized,
        "last_fill_px": last_fill,
        "ref_px": ref_px,
        "market_value": market_value,
        "unrealized_pnl": unrealized,
        "fills_applied": 1,
        "updated_ns": now_ns(),
    }


def feed_series(engine: AnalyticsEngine, equities) -> None:
    """Drive the engine through a sequence of single-position equity levels.

    The engine computes unrealized P&L as ``net_qty * (mark - avg_price)``, so
    we drive each target equity level with a real long position: qty=10,
    avg=10.0, and mark = eq/10 + 10.0 gives exactly ``unrealized = eq``.
    """
    for eq in equities:
        mark = eq / 10.0 + 10.0
        rows = [pos_row("EQSYM", net_qty=10, avg_price=10.0,
                        realized=0.0, last_fill=mark, ref_px=mark)]
        engine.ingest_positions(rows)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_validate_config_default_is_valid():
    assert validate_config(ServiceConfig()) == []


def test_validate_config_bad_port():
    errs = validate_config(ServiceConfig(listen_port=0))
    assert any("listen_port" in e for e in errs)


def test_validate_config_bad_url():
    cfg = ServiceConfig(ingest=IngestConfig(position_keeper_url="ftp://nope"))
    errs = validate_config(cfg)
    assert any("position_keeper_url" in e for e in errs)


def test_validate_config_bad_confidence():
    cfg = ServiceConfig(risk=RiskConfig(confidence=0.4))
    errs = validate_config(cfg)
    assert any("confidence" in e for e in errs)


def test_validate_config_bad_method():
    cfg = ServiceConfig(risk=RiskConfig(method="quantum"))
    errs = validate_config(cfg)
    assert any("risk.method" in e for e in errs)


def test_validate_config_min_samples():
    cfg = ServiceConfig(history=HistoryConfig(min_samples_for_var=1))
    errs = validate_config(cfg)
    assert any("min_samples_for_var" in e for e in errs)


# ---------------------------------------------------------------------------
# Model parsing
# ---------------------------------------------------------------------------

def test_position_row_from_dict_full():
    r = PositionRow.from_dict(pos_row("ES", net_qty=5, avg_price=100.0,
                                      realized=10.0, last_fill=101.0, ref_px=102.0))
    assert r.symbol == "ES"
    assert r.net_qty == 5
    assert r.avg_price == 100.0
    assert r.realized_pnl == 10.0
    assert r.ref_px == 102.0
    assert r.mark == 102.0          # ref preferred over last fill
    assert r.effective_market_value == 5 * 102.0
    assert r.effective_unrealized == 5 * (102.0 - 100.0)


def test_position_row_from_dict_fallback_mark():
    r = PositionRow.from_dict(pos_row("ES", net_qty=3, avg_price=10.0,
                                      last_fill=11.0, ref_px=None))
    assert r.ref_px is None
    assert r.mark == 11.0           # falls back to last fill
    assert r.effective_unrealized == 3 * (11.0 - 10.0)


def test_position_row_flat():
    r = PositionRow.from_dict(pos_row("ES", net_qty=0, avg_price=0.0))
    assert r.side == "FLAT"
    assert r.effective_unrealized == 0.0
    assert r.effective_market_value == 0.0


def test_position_row_bad_ref_ignored():
    r = PositionRow.from_dict(pos_row("ES", net_qty=1, last_fill=5.0, ref_px="garbage"))
    assert r.ref_px is None
    assert r.mark == 5.0


# ---------------------------------------------------------------------------
# Pure numeric helpers
# ---------------------------------------------------------------------------

def test_mean_and_stdev():
    assert _mean([1.0, 2.0, 3.0]) == 2.0
    assert _stdev_population([]) == 0.0
    assert _stdev_population([5.0]) == 0.0
    # population stdev of [1,2,3] is sqrt(2/3)
    assert math.isclose(_stdev_population([1.0, 2.0, 3.0]), math.sqrt(2.0 / 3.0))


def test_sharpe_zero_variance_is_none():
    # All identical returns -> zero stdev -> None (no infinite ratio).
    assert _sharpe([0.01, 0.01, 0.01], 0.0, 252) is None


def test_sharpe_insufficient_is_none():
    assert _sharpe([0.01], 0.0, 252) is None


def test_sharpe_positive_for_uptrend():
    rets = [0.01, -0.002, 0.015, 0.008, -0.001, 0.02]
    s = _sharpe(rets, 0.0, 252)
    assert s is not None
    assert s > 0


def test_sharpe_annualization_scales_with_periods():
    rets = [0.01, -0.002, 0.015, 0.008]
    s_low = _sharpe(rets, 0.0, 10)
    s_high = _sharpe(rets, 0.0, 40)
    assert s_high > s_low          # sqrt(periods) scaling


def test_max_drawdown_pct():
    eq = [100.0, 120.0, 90.0, 150.0, 75.0]
    # peak 120 -> trough 90: (90-120)/120 = -25%; peak 150 -> 75: -50%
    assert math.isclose(_max_drawdown_pct(eq), -50.0)


def test_max_drawdown_empty_and_flat():
    assert _max_drawdown_pct([]) == 0.0
    assert _max_drawdown_pct([100.0, 100.0, 100.0]) == 0.0


def test_current_drawdown_pct():
    eq = [100.0, 150.0, 120.0]     # peak 150, last 120 -> -20%
    assert math.isclose(_current_drawdown_pct(eq), -20.0)
    assert _current_drawdown_pct([]) == 0.0


def test_win_rate_pct():
    assert _win_rate_pct([]) is None
    assert math.isclose(_win_rate_pct([0.1, -0.1, 0.2, 0.0]), 50.0)


def test_annualized_return():
    # doubling over exactly one year (one period == one year) -> +100%
    assert math.isclose(_annualized_return([100.0, 200.0], 1.0), 1.0)
    # flat series -> 0% annualized
    assert math.isclose(_annualized_return([100.0, 100.0], 10), 0.0)
    assert _annualized_return([0.0, 100.0], 10) is None     # non-positive start
    assert _annualized_return([100.0], 10) is None          # too short


def test_historical_var_basic():
    # symmetric returns; VaR at 95% should be a positive loss number.
    rets = [-0.05, -0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05]
    v, cv = _historical_var(rets, 0.95)
    assert v is not None and cv is not None
    assert v > 0
    assert cv >= v                  # CVaR (tail mean loss) >= VaR


def test_historical_var_empty():
    assert _historical_var([], 0.95) == (None, None)


def test_parametric_var_matches_normal():
    rets = [0.01, -0.02, 0.03, -0.01, 0.02, -0.005, 0.0]
    v, cv = _parametric_var(rets, 0.95)
    assert v is not None and cv is not None
    # z(0.95) ~ 1.6449; var_loss = -(mean - z*sd)
    m = _mean(rets)
    sd = _stdev_population(rets)
    z = _norm_ppf(0.95)
    assert math.isclose(v, -(m - z * sd), rel_tol=1e-6)


def test_parametric_var_insufficient():
    assert _parametric_var([0.01], 0.95) == (None, None)


def test_norm_ppf_known_values():
    assert math.isclose(_norm_ppf(0.5), 0.0, abs_tol=1e-3)
    assert math.isclose(_norm_ppf(0.975), 1.96, abs_tol=1e-2)
    assert math.isclose(_norm_ppf(0.95), 1.645, abs_tol=1e-2)
    # symmetry
    assert math.isclose(_norm_ppf(0.05), -_norm_ppf(0.95), rel_tol=1e-3)


def test_norm_cdf_known_values():
    assert math.isclose(_norm_cdf(0.0), 0.5, abs_tol=1e-9)
    assert math.isclose(_norm_cdf(1.96), 0.975, abs_tol=1e-3)


# ---------------------------------------------------------------------------
# Engine: P&L
# ---------------------------------------------------------------------------

def test_ingest_positions_aggregates_pnl():
    eng = make_engine()
    rows = [
        pos_row("AAA", net_qty=10, avg_price=10.0, realized=5.0, last_fill=12.0, ref_px=12.0),
        pos_row("BBB", net_qty=-4, avg_price=50.0, realized=-3.0, last_fill=48.0, ref_px=48.0),
    ]
    n = eng.ingest_positions(rows)
    assert n == 2
    view = eng.pnl()
    assert view["symbols_total"] == 2
    # AAA: unrealized = 10*(12-10)=20, total=25 ; BBB: unrealized=-4*(48-50)=8, total=5
    tot = view["totals"]
    assert math.isclose(tot["realized_pnl"], 2.0)
    assert math.isclose(tot["unrealized_pnl"], 28.0)
    assert math.isclose(tot["total_pnl"], 30.0)


def test_ingest_empty_preserves_state():
    eng = make_engine()
    eng.ingest_positions([pos_row("AAA", net_qty=1, avg_price=1.0, realized=9.0)])
    before = eng.stats_view()["positions_tracked"]
    n = eng.ingest_positions([])          # no data this pass
    assert n == 0
    after = eng.stats_view()["positions_tracked"]
    assert before == after == 1           # state preserved, not wiped


def test_ingest_none_preserves_state():
    eng = make_engine()
    eng.ingest_positions([pos_row("AAA", net_qty=1, avg_price=1.0)])
    assert eng.ingest_positions(None) == 0
    assert eng.stats_view()["positions_tracked"] == 1


def test_ingest_skips_malformed_rows():
    eng = make_engine()
    # A row whose net_qty cannot be coerced to int raises in from_dict and is
    # skipped; the well-formed row still lands in the book.
    n = eng.ingest_positions([
        {"symbol": "OK", "net_qty": 1},
        {"symbol": "BAD", "net_qty": "not-an-int"},
    ])
    assert n == 1
    assert eng.pnl_symbol("OK") is not None


def test_pnl_symbol_known_and_unknown():
    eng = make_engine()
    eng.ingest_positions([pos_row("AAA", net_qty=2, avg_price=10.0, realized=1.0,
                                  last_fill=11.0, ref_px=11.0)])
    row = eng.pnl_symbol("AAA")
    assert row is not None
    assert math.isclose(row.total_pnl, 1.0 + 2 * (11.0 - 10.0))
    assert eng.pnl_symbol("ZZZ") is None


# ---------------------------------------------------------------------------
# Engine: attribution
# ---------------------------------------------------------------------------

def test_attribution_decomposition():
    eng = make_engine()
    rows = [
        pos_row("AAA", net_qty=10, avg_price=10.0, realized=5.0, last_fill=12.0, ref_px=12.0),  # total 25
        pos_row("BBB", net_qty=-4, avg_price=50.0, realized=-3.0, last_fill=48.0, ref_px=48.0),  # total 5
    ]
    eng.ingest_positions(rows)
    attr = eng.attribution()
    assert math.isclose(attr["grand_total"], 30.0)
    assert math.isclose(attr["realized_total"], 2.0)
    assert math.isclose(attr["unrealized_total"], 28.0)
    # largest contributor first
    assert attr["symbols"][0]["symbol"] == "AAA"
    # weights sum to ~100
    wsum = sum(s["weight_pct"] for s in attr["symbols"])
    assert math.isclose(wsum, 100.0, abs_tol=0.01)


def test_attribution_flat_book():
    eng = make_engine()
    eng.ingest_positions([pos_row("AAA", net_qty=0)])
    attr = eng.attribution()
    assert math.isclose(attr["grand_total"], 0.0)
    assert attr["symbols"][0]["weight_pct"] == 0.0


# ---------------------------------------------------------------------------
# Engine: metrics
# ---------------------------------------------------------------------------

def test_metrics_empty():
    eng = make_engine()
    m = eng.metrics()
    assert m.samples == 0
    assert m.returns == 0
    assert m.sharpe_ratio is None
    assert m.win_rate_pct is None


def test_metrics_after_series():
    eng = make_engine()
    feed_series(eng, [100.0, 110.0, 105.0, 120.0, 130.0])
    m = eng.metrics()
    assert m.samples == 5
    assert m.returns == 4
    assert m.win_rate_pct is not None
    # equity rose overall -> current drawdown <= 0 (may be 0 at the peak)
    assert m.current_drawdown_pct <= 0.0
    # max drawdown: 110->105 is -4.545%
    assert math.isclose(m.max_drawdown_pct, (105.0 - 110.0) / 110.0 * 100.0, rel_tol=1e-6)


def test_metrics_sharpe_gate():
    cfg = ServiceConfig(metrics=MetricsConfig(min_returns_for_sharpe=5))
    eng = make_engine()
    feed_series(eng, [100.0, 101.0, 102.0])   # only 2 returns < gate of 5
    m = eng.metrics()
    assert m.sharpe_ratio is None             # gated by min_returns_for_sharpe


def test_metrics_annualized_return_present():
    # Use a small periods_per_day so 2 periods spans >= 1 year (annualizable).
    eng = make_engine(metrics=MetricsConfig(periods_per_day=2))
    feed_series(eng, [100.0, 120.0, 150.0])
    m = eng.metrics()
    assert m.annualized_return is not None
    assert m.annualized_return > 0


# ---------------------------------------------------------------------------
# Engine: VaR
# ---------------------------------------------------------------------------

def test_var_insufficient_data():
    cfg = ServiceConfig(history=HistoryConfig(min_samples_for_var=30))
    eng = make_engine()
    feed_series(eng, [100.0, 101.0, 102.0])   # only 2 returns < 30
    r = eng.var()
    assert r.insufficient_data is True
    assert r.var is None


def test_var_historical_sufficient():
    cfg = ServiceConfig(history=HistoryConfig(min_samples_for_var=5))
    eng = make_engine(cfg)
    # build a deterministic return series via equity levels
    feed_series(eng, [100.0, 98.0, 102.0, 97.0, 103.0, 96.0, 104.0])
    r = eng.var(method="historical", confidence=0.95)
    assert r.insufficient_data is False
    assert r.method == "historical"
    assert r.var is not None and r.var > 0
    assert r.cvar is not None and r.cvar >= r.var


def test_var_parametric_sufficient():
    cfg = ServiceConfig(history=HistoryConfig(min_samples_for_var=5))
    eng = make_engine(cfg)
    feed_series(eng, [100.0, 98.0, 102.0, 97.0, 103.0, 96.0, 104.0])
    r = eng.var(method="parametric", confidence=0.95)
    assert r.insufficient_data is False
    assert r.method == "parametric"
    assert r.var is not None


def test_var_default_uses_config():
    cfg = ServiceConfig(risk=RiskConfig(method="parametric", confidence=0.9),
                        history=HistoryConfig(min_samples_for_var=5))
    eng = make_engine(cfg)
    feed_series(eng, [100.0, 98.0, 102.0, 97.0, 103.0, 96.0])
    r = eng.var()
    assert r.method == "parametric"
    assert math.isclose(r.confidence, 0.9)


# ---------------------------------------------------------------------------
# Engine: history & stats
# ---------------------------------------------------------------------------

def test_history_newest_first_and_bounded():
    cfg = ServiceConfig(history=HistoryConfig(max_history_points=3))
    eng = make_engine(cfg)
    feed_series(eng, [100.0, 110.0, 120.0, 130.0])   # cap at 3
    h = eng.history(limit=10)
    assert len(h) == 3                    # bounded by max_history_points
    # newest first -> last equity (130) is the highest sample value here
    assert h[0]["equity"] >= h[-1]["equity"]


def test_stats_view_counts():
    eng = make_engine()
    feed_series(eng, [100.0, 110.0])
    s = eng.stats_view()
    assert s["positions_tracked"] == 1
    assert s["equity_samples"] == 2
    assert s["returns_observed"] == 1
    assert s["refreshes_completed"] == 2


def test_readiness_reasons():
    eng = make_engine()
    assert len(eng.readiness_reasons()) == 1
    feed_series(eng, [100.0])
    assert eng.readiness_reasons() == []


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------

def test_error_envelope_known_symbol():
    env = error_envelope(UnknownSymbolError("XYZ"))
    e = env["error"]
    assert e["code"] == "PFA-203"
    assert e["service"] == "portfolio-analytics"
    assert e["retryable"] is False
    assert e["context"]["symbol"] == "XYZ"


def test_error_envelope_upstream_retryable():
    env = error_envelope(UpstreamUnreachableError("position-keeper", "timeout"))
    assert env["error"]["code"] == "PFA-301"
    assert env["error"]["retryable"] is True


def test_error_envelope_insufficient_data():
    env = error_envelope(InsufficientDataError("var", 5, 30))
    e = env["error"]
    assert e["code"] == "PFA-402"
    assert e["context"]["need"] == 30
    assert e["context"]["have"] == 5


def test_error_envelope_unknown_exception():
    env = error_envelope(ValueError("boom"))
    assert env["error"]["code"] == "PFA-999"
    assert env["error"]["service"] == "portfolio-analytics"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def test_router_routes_and_404():
    from pfa.controller import AnalyticsController
    ctrl = AnalyticsController()
    ctrl.engine = make_engine()
    ctrl.engine.ingest_positions([pos_row("AAA", net_qty=1, avg_price=1.0,
                                          realized=2.0, last_fill=3.0, ref_px=3.0)])
    router = build_router(ctrl)

    status, body = router.dispatch("GET", "/healthz", {})
    assert status == 200 and body["status"] == "ok"

    status, body = router.dispatch("GET", "/pnl", {})
    assert status == 200 and body["symbols_total"] == 1

    status, body = router.dispatch("GET", "/pnl/AAA", {})
    assert status == 200 and body["pnl"]["symbol"] == "AAA"

    status, body = router.dispatch("GET", "/pnl/NOPE", {})
    assert status == 404 and body["error"]["code"] == "PFA-203"

    status, body = router.dispatch("GET", "/var", {"method": "parametric"})
    assert status == 200 and body["var"]["method"] == "parametric"

    status, body = router.dispatch("GET", "/does-not-exist", {})
    assert status == 404 and body["error"]["code"] == "PFA-404"


def test_router_var_confidence_parse():
    from pfa.controller import AnalyticsController
    ctrl = AnalyticsController()
    eng = make_engine()
    feed_series(eng, [100.0, 98.0, 102.0, 97.0, 103.0])
    # lower the var gate so we get a real number
    eng.cfg = ServiceConfig(history=HistoryConfig(min_samples_for_var=4))
    ctrl.engine = eng
    router = build_router(ctrl)
    status, body = router.dispatch("GET", "/var", {"confidence": "0.9"})
    assert status == 200
    assert math.isclose(body["var"]["confidence"], 0.9)
