"""position_keeper — unit tests for the position ledger and HTTP surface.

Run with:  cd services/position-keeper && python -m pytest tests/ -v
No network calls; everything runs in-memory against a fresh PositionEngine,
and the HTTP layer is exercised through the router directly (no sockets).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `posk` importable without installation (mirrors other services' tests).
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from posk.config import (  # noqa: E402
    AccountingConfig,
    HistoryConfig,
    IngestConfig,
    ServiceConfig,
    SnapshotConfig,
    validate_config,
)
from posk.controller import PositionController  # noqa: E402
from posk.errors import (  # noqa: E402
    AdjustmentConflictError,
    DuplicateFillError,
    InvalidAdjustmentError,
    InvalidCorporateActionError,
    UnknownPositionError,
    error_envelope,
)
from posk.models import (  # noqa: E402
    AdjustmentRecord,
    CorporateAction,
    EventType,
    FillEvent,
    PositionSide,
    PositionState,
    now_ns,
)
from posk.position_engine import PositionEngine  # noqa: E402
from posk.router import build_router  # noqa: E402


def make_cfg(**kw) -> ServiceConfig:
    defaults = {
        "ingest": IngestConfig(),
        "history": HistoryConfig(),
        "snapshot": SnapshotConfig(),
        "accounting": AccountingConfig(),
    }
    defaults.update(kw)
    return ServiceConfig(**defaults)


def make_engine(**cfg_overrides) -> PositionEngine:
    return PositionEngine(make_cfg(**cfg_overrides))


def fill(fid="F1", symbol="X", side=PositionSide.BUY, qty=10, price=100.0,
         account="MAIN", ts=None) -> FillEvent:
    return FillEvent(id=fid, order_id=f"O-{fid}", symbol=symbol, venue="SIM",
                     side=side, qty=qty, price=price,
                     ts_ns=int(ts or now_ns()), account=account)


def controller_for(engine: PositionEngine) -> PositionController:
    c = PositionController()
    c.engine = engine
    return c


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_validate_config_defaults_valid():
    assert validate_config(ServiceConfig()) == []


def test_validate_config_catches_bad_values():
    cfg = make_cfg(accounting=AccountingConfig(default_account="", base_currency="US"))
    errors = validate_config(cfg)
    assert any("default_account" in e for e in errors)
    assert any("base_currency" in e for e in errors)


def test_validate_config_bad_url():
    cfg = make_cfg(ingest=IngestConfig(execution_gateway_url="not-a-url"))
    assert any("execution_gateway_url" in e for e in validate_config(cfg))


# ---------------------------------------------------------------------------
# Fill parsing
# ---------------------------------------------------------------------------

def test_fill_from_dict_aliases():
    f = FillEvent.from_dict({"id": "F9", "sym": "Y", "side": "sell", "qty": 3,
                             "px": 9.5, "vt": 123}, default_account="MAIN")
    assert f.symbol == "Y" and f.side is PositionSide.SELL
    assert f.qty == 3 and abs(f.price - 9.5) < 1e-9 and f.ts_ns == 123


def test_fill_from_dict_rejects_bad_side():
    try:
        FillEvent.from_dict({"id": "F", "symbol": "X", "side": "HOLD", "qty": 1, "price": 1.0},
                            default_account="MAIN")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_fill_from_dict_rejects_nonpositive_qty():
    try:
        FillEvent.from_dict({"id": "F", "symbol": "X", "side": "BUY", "qty": 0, "price": 1.0},
                            default_account="MAIN")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Position accounting (model-level)
# ---------------------------------------------------------------------------

def test_position_open_and_add():
    pos = PositionState(account="A", symbol="X")
    r1 = pos.apply_fill(PositionSide.BUY, 10, 100.0, now_ns())
    assert r1 == 0.0 and pos.net_qty == 10 and abs(pos.avg_price - 100.0) < 1e-9
    pos.apply_fill(PositionSide.BUY, 10, 200.0, now_ns())
    assert pos.net_qty == 20 and abs(pos.avg_price - 150.0) < 1e-9


def test_position_reduce_realizes_pnl():
    pos = PositionState(account="A", symbol="X")
    pos.apply_fill(PositionSide.BUY, 10, 100.0, now_ns())
    realized = pos.apply_fill(PositionSide.SELL, 4, 120.0, now_ns())
    assert pos.net_qty == 6 and abs(pos.avg_price - 100.0) < 1e-9
    assert abs(realized - 80.0) < 1e-9          # (120-100)*4
    assert abs(pos.realized_pnl - 80.0) < 1e-9


def test_position_flip():
    pos = PositionState(account="A", symbol="X")
    pos.apply_fill(PositionSide.BUY, 10, 100.0, now_ns())
    realized = pos.apply_fill(PositionSide.SELL, 14, 130.0, now_ns())
    # close 10 long @100 -> +300 ; open 4 short @130
    assert pos.net_qty == -4 and abs(pos.avg_price - 130.0) < 1e-9
    assert abs(realized - 300.0) < 1e-9


def test_position_zero_qty_noop():
    pos = PositionState(account="A", symbol="X")
    pos.apply_fill(PositionSide.BUY, 5, 100.0, now_ns())
    before = (pos.net_qty, pos.avg_price, pos.realized_pnl)
    r = pos.apply_fill(PositionSide.SELL, 0, 999.0, now_ns())
    assert r == 0.0 and (pos.net_qty, pos.avg_price, pos.realized_pnl) == before


# ---------------------------------------------------------------------------
# Engine: fill application + idempotency
# ---------------------------------------------------------------------------

def test_engine_apply_fill_creates_position():
    eng = make_engine()
    pos = eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    assert pos.net_qty == 10 and abs(pos.avg_price - 100.0) < 1e-9
    assert eng.stats_view()["fills_applied"] == 1


def test_engine_duplicate_fill_rejected():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1"))
    try:
        eng.apply_fill(fill(fid="F1"))
        assert False, "expected DuplicateFillError"
    except DuplicateFillError:
        pass
    assert eng.stats_view()["fills_deduplicated"] == 1


def test_engine_separate_accounts():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", account="A"))
    eng.apply_fill(fill(fid="F2", account="B"))
    a = eng.get_position("A", "X")
    b = eng.get_position("B", "X")
    assert a.net_qty == 10 and b.net_qty == 10
    assert len(eng.list_positions()) == 2


def test_engine_unknown_position_404():
    eng = make_engine()
    try:
        eng.get_position("MAIN", "NOPE")
        assert False, "expected UnknownPositionError"
    except UnknownPositionError:
        pass


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------

def test_adjustment_requires_existing_position():
    eng = make_engine()
    try:
        eng.apply_adjustment(AdjustmentRecord(id="A1", account="MAIN", symbol="X", delta_qty=5))
        assert False, "expected UnknownPositionError"
    except UnknownPositionError:
        pass


def test_adjustment_applies():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    pos = eng.apply_adjustment(AdjustmentRecord(id="A1", account="MAIN", symbol="X",
                                                delta_qty=-4, reason="correction"))
    assert pos.net_qty == 6 and abs(pos.avg_price - 100.0) < 1e-9
    assert pos.adjustments_applied == 1


def test_adjustment_zero_delta_rejected():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1"))
    try:
        eng.apply_adjustment_dict({"symbol": "X", "delta_qty": 0})
        assert False, "expected InvalidAdjustmentError"
    except InvalidAdjustmentError:
        pass


# ---------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------

def test_split_corporate_action():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    affected = eng.apply_corporate_action(CorporateAction(type="SPLIT", symbol="X", factor=2.0))
    assert len(affected) == 1
    pos = affected[0]
    assert pos.net_qty == 20 and abs(pos.avg_price - 50.0) < 1e-9


def test_dividend_corporate_action():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    eng.apply_corporate_action(CorporateAction(type="DIVIDEND", symbol="X", per_share=2.5))
    pos = eng.get_position("MAIN", "X")
    assert abs(pos.realized_pnl - 25.0) < 1e-9   # 10 lots * 2.5


def test_corporate_action_bad_type_rejected():
    try:
        CorporateAction.from_dict({"type": "MERGER", "symbol": "X"})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_corporate_action_no_position_404():
    eng = make_engine()
    try:
        eng.apply_corporate_action(CorporateAction(type="SPLIT", symbol="NOPE", factor=2.0))
        assert False, "expected UnknownPositionError"
    except UnknownPositionError:
        pass


# ---------------------------------------------------------------------------
# Snapshots & history
# ---------------------------------------------------------------------------

def test_snapshot_marks_to_market():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    eng.set_reference_price("X", 110.0)
    snap = eng.take_snapshot()
    assert snap.totals["positions"] == 1
    assert abs(snap.totals["market_value"] - 1100.0) < 1e-6
    row = snap.positions[0]
    assert abs(row["unrealized_pnl"] - 100.0) < 1e-6   # 10*(110-100)


def test_history_records_events():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    eng.apply_adjustment(AdjustmentRecord(id="A1", account="MAIN", symbol="X", delta_qty=-2))
    events = eng.history("X")
    kinds = [e.kind for e in events]
    assert EventType.FILL in kinds and EventType.ADJUSTMENT in kinds
    # newest first
    assert events[0].kind is EventType.ADJUSTMENT


def test_history_bounded():
    cfg = make_cfg(history=HistoryConfig(max_events_per_symbol=5))
    eng = PositionEngine(cfg)
    for i in range(10):
        eng.apply_fill(fill(fid=f"F{i}", qty=1, price=10.0 + i))
    assert len(eng.history("X")) == 5


# ---------------------------------------------------------------------------
# HTTP surface (router-level, no sockets)
# ---------------------------------------------------------------------------

def test_router_healthz_and_readyz():
    eng = make_engine()
    router = build_router(controller_for(eng))
    status, body = router.dispatch("GET", "/healthz", {})
    assert status == 200 and body["service"] == "position-keeper"
    status, body = router.dispatch("GET", "/readyz", {})
    assert status == 200 and body["status"] == "ready"


def test_router_positions_and_symbol():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    router = build_router(controller_for(eng))

    status, body = router.dispatch("GET", "/positions", {})
    assert status == 200 and body["count"] == 1
    assert body["positions"][0]["net_qty"] == 10

    status, body = router.dispatch("GET", "/positions/X", {})
    assert status == 200 and body["symbol"] == "X" and body["net_qty"] == 10

    status, body = router.dispatch("GET", "/positions/NOPE", {})
    assert status == 404 and body["error"]["code"] == "POS-203"


def test_router_snapshot_route_not_swallowed():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    router = build_router(controller_for(eng))
    status, body = router.dispatch("GET", "/positions/snapshot", {})
    assert status == 200 and "totals" in body and "positions" in body


def test_router_adjust_endpoint():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    router = build_router(controller_for(eng))
    status, body = router.dispatch("POST", "/adjust", {},
                                   body={"symbol": "X", "delta_qty": -5})
    assert status == 200 and body["position"]["net_qty"] == 5

    # missing symbol -> 400
    status, body = router.dispatch("POST", "/adjust", {}, body={})
    assert status == 400 and body["error"]["code"] == "POS-201"


def test_router_corporate_action_endpoint():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    router = build_router(controller_for(eng))
    status, body = router.dispatch("POST", "/corporate-action", {},
                                   body={"type": "SPLIT", "symbol": "X", "factor": 3.0})
    assert status == 200 and body["positions_affected"] == 1
    assert body["positions"][0]["net_qty"] == 30


def test_router_history_endpoint():
    eng = make_engine()
    eng.apply_fill(fill(fid="F1", qty=10, price=100.0))
    router = build_router(controller_for(eng))
    status, body = router.dispatch("GET", "/history/X", {})
    assert status == 200 and body["count"] >= 1


def test_router_404_envelope():
    eng = make_engine()
    router = build_router(controller_for(eng))
    status, body = router.dispatch("GET", "/nope", {})
    assert status == 404 and body["error"]["code"] == "POS-404"


# ---------------------------------------------------------------------------
# Error envelope shape
# ---------------------------------------------------------------------------

def test_error_envelope_shape():
    from posk.errors import POSKError
    env = error_envelope(POSKError("boom"))
    e = env["error"]
    assert set(e) == {"code", "message", "service", "retryable", "context"}
    assert e["service"] == "position-keeper"
    assert e["code"].startswith("POS-")


def test_duplicate_fill_envelope():
    env = error_envelope(DuplicateFillError("F1"))
    assert env["error"]["code"] == "POS-205"
    assert env["error"]["context"]["fill_id"] == "F1"
