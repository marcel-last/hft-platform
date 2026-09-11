"""order_book_builder — unit tests for the book engine."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obb.book_engine import BookEngine
from obb.models import BookEventKind, BookHealth, OrderBook, PriceLevel, TopOfBook, now_ns


def make_engine():
    return BookEngine(tick_sizes={"TEST_SYM": 0.5})


def wire(sym="TEST_SYM", ven="V1", act="N", side="BID", px=100.0, qty=10, lvl=1):
    return {"sym": sym, "ven": ven, "act": act, "side": side, "px": px, "qty": qty, "lvl": lvl}


def test_new_quote_creates_book():
    eng = make_engine()
    events = eng.apply_quote(wire(act="N", side="BID", px=100.0, qty=5))
    book = eng.get("TEST_SYM", "V1")
    assert book is not None
    assert book.messages_applied == 1
    assert 100.0 in book.bids
    assert book.bids[100.0].quantity == 5


def test_modify_updates_quantity():
    eng = make_engine()
    eng.apply_quote(wire(act="N", side="BID", px=100.0, qty=5))
    eng.apply_quote(wire(act="M", side="BID", px=100.0, qty=8))
    book = eng.get("TEST_SYM", "V1")
    assert book.bids[100.0].quantity == 8
    # updates counter increments on M/E only (not on initial N)
    assert book.bids[100.0].updates == 1


def test_delete_removes_level():
    eng = make_engine()
    eng.apply_quote(wire(act="N", side="BID", px=100.0, qty=5))
    eng.apply_quote(wire(act="D", side="BID", px=100.0, qty=0))
    book = eng.get("TEST_SYM", "V1")
    assert 100.0 not in book.bids


def test_execute_reduces_quantity():
    eng = make_engine()
    eng.apply_quote(wire(act="N", side="ASK", px=101.0, qty=20))
    eng.apply_quote(wire(act="E", side="ASK", px=101.0, qty=7))
    book = eng.get("TEST_SYM", "V1")
    assert book.asks[101.0].quantity == 13


def test_execute_floors_at_zero_and_deletes():
    eng = make_engine()
    eng.apply_quote(wire(act="N", side="ASK", px=101.0, qty=5))
    eng.apply_quote(wire(act="E", side="ASK", px=101.0, qty=10))
    book = eng.get("TEST_SYM", "V1")
    assert 101.0 not in book.asks


def test_cross_detection():
    eng = make_engine()
    eng.apply_quote(wire(act="N", side="BID", px=100.0, qty=5))
    eng.apply_quote(wire(act="N", side="ASK", px=99.0, qty=5))  # crossed!
    book = eng.get("TEST_SYM", "V1")
    assert book.cross_events == 1


def test_top_of_book_computation():
    eng = make_engine()
    eng.apply_quote(wire(act="N", side="BID", px=99.5, qty=10))
    eng.apply_quote(wire(act="N", side="ASK", px=100.5, qty=8))
    book = eng.get("TEST_SYM", "V1")
    tob = book.top_of_book()
    assert tob.best_bid_price == 99.5
    assert tob.best_ask_price == 100.5
    assert tob.mid_price == 100.0
    assert tob.spread == 1.0
    assert tob.spread_ticks == 2  # 1.0 / 0.5 = 2 ticks


def test_sorted_views_ordering():
    eng = make_engine()
    eng.apply_quote(wire(act="N", side="BID", px=98.0, qty=5))
    eng.apply_quote(wire(act="N", side="BID", px=100.0, qty=3))
    eng.apply_quote(wire(act="N", side="BID", px=99.0, qty=7))
    book = eng.get("TEST_SYM", "V1")
    sb = book.sorted_bids()
    assert [l.price for l in sb] == [100.0, 99.0, 98.0]


def test_rebuild_from_snapshot():
    eng = make_engine()
    eng.apply_quote(wire(act="N", side="BID", px=100.0, qty=5))
    book = eng.rebuild_from_snapshot(
        "TEST_SYM", "V1",
        bids=[[99.0, 20], [98.0, 30]],
        asks=[[101.0, 15]],
    )
    assert book.rebuilds == 1
    assert set(book.bids.keys()) == {99.0, 98.0}
    assert set(book.asks.keys()) == {101.0}
    assert book.health == BookHealth.HEALTHY


def test_orphan_delete_tolerated():
    eng = make_engine()
    eng.apply_quote(wire(act="D", side="BID", px=999.0, qty=0))  # no level exists
    assert eng.stats["orphan_deletes"] == 1


def test_imbalance_event():
    eng = make_engine()
    # Set up an imbalanced book: large bid, small ask at best levels
    eng.apply_quote(wire(act="N", side="BID", px=100.0, qty=50))
    eng.apply_quote(wire(act="N", side="ASK", px=101.0, qty=10))
    book = eng.get("TEST_SYM", "V1")
    tob = book.top_of_book()
    assert tob.imbalance_ratio == 5.0
    # Total bid qty includes all levels
    assert book.total_bid_qty() == 50


def test_snapshot_serialization():
    eng = make_engine()
    eng.apply_quote(wire(act="N", side="BID", px=100.0, qty=5))
    eng.apply_quote(wire(act="N", side="ASK", px=101.0, qty=3))
    book = eng.get("TEST_SYM", "V1")
    snap = book.to_snapshot_dict(depth=10)
    assert snap["sym"] == "TEST_SYM"
    assert snap["ven"] == "V1"
    assert len(snap["bids"]) == 1
    assert len(snap["asks"]) == 1
    assert snap["tob"]["bb_px"] == 100.0
    assert snap["tob"]["ba_px"] == 101.0


def test_last_side_ignored():
    eng = make_engine()
    events = eng.apply_quote(wire(act="N", side="LAST", px=100.0, qty=5))
    book = eng.get("TEST_SYM", "V1")
    assert book is None or book.messages_applied == 0
