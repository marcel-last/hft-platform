"""risk_manager — unit tests for the risk engine and pre-trade pipeline.

Run with:  cd services/risk-manager && python -m pytest tests/ -v
No network calls; everything runs in-memory against a fresh RiskEngine.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `rkm` importable without installation (mirrors other services' tests).
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rkm.config import (  # noqa: E402
    IngestConfig,
    KillSwitchConfig,
    LimitsConfig,
    ServiceConfig,
    VelocityConfig,
    validate_config,
)
from rkm.errors import (  # noqa: E402
    KillSwitchAlreadyEngagedError,
    KillSwitchNotEngagedError,
    error_envelope,
)
from rkm.models import (  # noqa: E402
    BreachSeverity,
    KillSwitchState,
    OrderRecord,
    PreTradeRequest,
    PositionState,
    RiskSide,
    now_ns,
)
from rkm.risk_engine import RiskEngine  # noqa: E402


def make_cfg(limits=None, velocity=None, kill_switch=None, ingest=None) -> ServiceConfig:
    return ServiceConfig(
        limits=limits or LimitsConfig(),
        velocity=velocity or VelocityConfig(),
        kill_switch=kill_switch or KillSwitchConfig(),
        ingest=ingest or IngestConfig(),
    )


def make_engine(**cfg_overrides) -> RiskEngine:
    return RiskEngine(make_cfg(**cfg_overrides))


def req(symbol="EU_STOXX50_CONT", side=RiskSide.BUY, qty=10, px=5_000.0, **kw):
    return PreTradeRequest(id=kw.pop("id", "PRT-1"), canonical_symbol=symbol,
                           side=side, qty=qty, limit_price=px, **kw)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_validate_config_defaults_valid():
    assert validate_config(ServiceConfig()) == []


def test_validate_config_catches_bad_values():
    cfg = make_cfg(limits=LimitsConfig(max_position_qty=0, max_order_notional=-1))
    errors = validate_config(cfg)
    assert any("max_position_qty" in e for e in errors)
    assert any("max_order_notional" in e for e in errors)


def test_validate_config_bad_url():
    cfg = make_cfg(kill_switch=KillSwitchConfig(escalation_url="not-a-url"))
    assert any("escalation_url" in e for e in validate_config(cfg))


# ---------------------------------------------------------------------------
# Position accounting
# ---------------------------------------------------------------------------

def test_position_open_and_add():
    pos = PositionState(canonical_symbol="X")
    pos.apply_fill(RiskSide.BUY, 10, 100.0, now_ns())
    assert pos.net_qty == 10 and pos.avg_price == 100.0
    pos.apply_fill(RiskSide.BUY, 10, 200.0, now_ns())
    assert pos.net_qty == 20 and abs(pos.avg_price - 150.0) < 1e-9


def test_position_reduce_and_flip():
    pos = PositionState(canonical_symbol="X")
    pos.apply_fill(RiskSide.BUY, 10, 100.0, now_ns())
    pos.apply_fill(RiskSide.SELL, 4, 120.0, now_ns())   # reduce long
    assert pos.net_qty == 6 and abs(pos.avg_price - 100.0) < 1e-9
    pos.apply_fill(RiskSide.SELL, 8, 130.0, now_ns())   # flip: close 6, open short 2
    assert pos.net_qty == -2 and abs(pos.avg_price - 130.0) < 1e-9


def test_position_zero_qty_noop():
    pos = PositionState(canonical_symbol="X")
    pos.apply_fill(RiskSide.BUY, 5, 100.0, now_ns())
    before = (pos.net_qty, pos.avg_price)
    pos.apply_fill(RiskSide.SELL, 0, 999.0, now_ns())
    assert (pos.net_qty, pos.avg_price) == before


# ---------------------------------------------------------------------------
# Pre-trade checks
# ---------------------------------------------------------------------------

def test_pre_trade_allowed_within_limits():
    eng = make_engine()
    result = eng.pre_trade_check(req(qty=10, px=5_000.0))
    assert result.allowed is True
    assert result.breached is False
    assert all(v.passed for v in result.verdicts)


def test_pre_trade_veto_order_qty():
    eng = make_engine()
    result = eng.pre_trade_check(req(qty=10_000, px=5.0))  # qty > max_order_qty(100)
    assert result.allowed is False
    assert result.hard_breached is True
    names = {v.name for v in result.verdicts if not v.passed}
    assert "max_order_qty" in names


def test_pre_trade_veto_position_limit():
    eng = make_engine()
    eng.apply_fill("EU_STOXX50_CONT", RiskSide.BUY, 95, 5_000.0, now_ns())
    result = eng.pre_trade_check(req(qty=10, px=5_000.0))  # projected 105 > 100
    assert result.allowed is False
    names = {v.name for v in result.verdicts if not v.passed}
    assert "position_limit" in names


def test_pre_trade_soft_notional_warning():
    eng = make_engine(limits=LimitsConfig(max_position_qty=10_000))
    # Position near the per-symbol notional limit so a small add crosses 80%.
    eng.apply_fill("X", RiskSide.BUY, 960, 5_000.0, now_ns())   # 4.8M of 5M (96%)
    result = eng.pre_trade_check(req(symbol="X", qty=1, px=5_000.0))
    soft = [v for v in result.verdicts if not v.passed and v.severity is BreachSeverity.SOFT]
    assert any(v.name == "notional_limit" for v in soft)
    assert result.allowed is True  # SOFT does not veto


def test_pre_trade_veto_portfolio_notional():
    eng = make_engine(limits=LimitsConfig(max_portfolio_notional=1_000_000.0))
    # Seed existing positions worth 950k of gross notional (positions are the
    # authoritative exposure; pre-trade checks do not mutate them).
    eng.apply_fill("A", RiskSide.BUY, 95, 10_000.0, now_ns())   # 950k
    eng.set_reference_price("A", 10_000.0)
    # A small new order stays under the cap.
    result = eng.pre_trade_check(req(symbol="B", qty=1, px=5_000.0))
    assert result.allowed is True
    # A large new order pushes projected portfolio notional past 1M -> vetoed.
    result2 = eng.pre_trade_check(req(symbol="C", qty=20, px=5_000.0))  # +100k => 1.05M
    names = {v.name for v in result2.verdicts if not v.passed}
    assert "portfolio_notional" in names


def test_pre_trade_velocity_veto():
    eng = make_engine(velocity=VelocityConfig(window_ms=60_000, max_orders_per_symbol=3, max_orders_total=10))
    r1 = eng.pre_trade_check(req(qty=1, px=5.0))
    r2 = eng.pre_trade_check(req(qty=1, px=5.0))
    r3 = eng.pre_trade_check(req(qty=1, px=5.0))
    assert r1.allowed and r2.allowed and r3.allowed
    r4 = eng.pre_trade_check(req(qty=1, px=5.0))  # 4th in window > 3
    assert r4.allowed is False
    names = {v.name for v in r4.verdicts if not v.passed}
    assert "velocity_symbol" in names


def test_pre_trade_open_order_cap():
    eng = make_engine(kill_switch=KillSwitchConfig(max_open_orders=2, auto_engage_on_hard_breach=False))
    eng.add_open_order(OrderRecord("O1", "A", RiskSide.BUY, 1, 5.0, "NEW"))
    eng.add_open_order(OrderRecord("O2", "B", RiskSide.SELL, 1, 5.0, "NEW"))
    result = eng.pre_trade_check(req(qty=1, px=5.0))  # would be 3rd open order
    assert result.allowed is False
    names = {v.name for v in result.verdicts if not v.passed}
    assert "max_open_orders" in names


def test_pre_trade_price_sanity():
    eng = make_engine(limits=LimitsConfig(min_price=1.0))
    result = eng.pre_trade_check(req(qty=1, px=0.5))  # below min price
    assert result.allowed is False
    names = {v.name for v in result.verdicts if not v.passed}
    assert "price_sanity" in names


# ---------------------------------------------------------------------------
# Limits management
# ---------------------------------------------------------------------------

def test_update_limits_partial_and_override():
    eng = make_engine()
    eng.update_limits({"max_position_qty": 50,
                       "symbol_overrides": {"X": {"max_position_qty": 200}}})
    assert eng.limits.max_position_qty == 50
    assert eng.limits.position_limit("X") == 200
    assert eng.limits.position_limit("Y") == 50


def test_reset_limits():
    eng = make_engine()
    eng.update_limits({"max_order_qty": 7})
    assert eng.limits.max_order_qty == 7
    eng.reset_limits()
    assert eng.limits.max_order_qty == 100  # default


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------

def test_kill_switch_engage_blocks_orders():
    eng = make_engine()
    eng.engage_kill_switch("test")
    assert eng.kill_switch_state is KillSwitchState.ENGAGED
    result = eng.pre_trade_check(req(qty=1, px=5.0))
    assert result.allowed is False
    names = {v.name for v in result.verdicts if not v.passed}
    assert "kill_switch" in names


def test_kill_switch_double_engage_raises():
    eng = make_engine()
    eng.engage_kill_switch("a")
    try:
        eng.engage_kill_switch("b")
        assert False, "expected KillSwitchAlreadyEngagedError"
    except KillSwitchAlreadyEngagedError:
        pass


def test_kill_switch_disengage_allows_again():
    eng = make_engine()
    eng.engage_kill_switch("a")
    eng.disengage_kill_switch()
    assert eng.kill_switch_state is KillSwitchState.DISARMED
    result = eng.pre_trade_check(req(qty=1, px=5.0))
    assert result.allowed is True


def test_kill_switch_disengage_when_disarmed_raises():
    eng = make_engine()
    try:
        eng.disengage_kill_switch()
        assert False, "expected KillSwitchNotEngagedError"
    except KillSwitchNotEngagedError:
        pass


def test_auto_engage_on_hard_breach():
    eng = make_engine(kill_switch=KillSwitchConfig(auto_engage_on_hard_breach=True))
    result = eng.pre_trade_check(req(qty=10_000, px=5.0))  # hard breach (order qty)
    assert result.allowed is False
    assert eng.kill_switch_state is KillSwitchState.ENGAGED


# ---------------------------------------------------------------------------
# Breaches / exposure / stats
# ---------------------------------------------------------------------------

def test_breaches_recorded_and_filtered():
    eng = make_engine()
    eng.pre_trade_check(req(qty=10_000, px=5.0))  # hard breach
    all_b = eng.breaches(limit=10)
    assert len(all_b) >= 1
    hard = eng.breaches(limit=10, severity="HARD")
    assert all(b.severity is BreachSeverity.HARD for b in hard)


def test_exposure_snapshot():
    eng = make_engine()
    eng.apply_fill("A", RiskSide.BUY, 10, 100.0, now_ns())
    eng.set_reference_price("A", 110.0)
    exp = eng.exposure()
    assert exp["gross_notional"] == 1_100.0
    pos_a = next(p for p in exp["positions"] if p["symbol"] == "A")
    assert pos_a["net_qty"] == 10 and pos_a["market_value"] == 1_100.0


def test_stats_counters():
    eng = make_engine()
    eng.pre_trade_check(req(qty=1, px=5.0))          # allowed
    eng.pre_trade_check(req(qty=10_000, px=5.0))    # vetoed
    s = eng.stats_view()
    assert s["pre_trade_checks"] == 2
    assert s["allowed"] == 1 and s["vetoed"] == 1


# ---------------------------------------------------------------------------
# PreTradeRequest parsing + error envelope
# ---------------------------------------------------------------------------

def test_pre_trade_request_from_dict_aliases():
    r = PreTradeRequest.from_dict({"sym": "X", "side": "sell", "qty": 3, "px": 9.5})
    assert r.canonical_symbol == "X" and r.side is RiskSide.SELL
    assert r.qty == 3 and abs(r.limit_price - 9.5) < 1e-9


def test_pre_trade_request_rejects_bad_side():
    try:
        PreTradeRequest.from_dict({"symbol": "X", "side": "HOLD", "qty": 1, "px": 1.0})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_error_envelope_shape():
    from rkm.errors import RKMError
    env = error_envelope(RKMError("boom"))
    e = env["error"]
    assert set(e) == {"code", "message", "service", "retryable", "context"}
    assert e["service"] == "risk-manager"
    assert e["code"].startswith("RKM-")
