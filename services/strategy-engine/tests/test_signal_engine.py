"""Unit tests for the strategy engine (S3).

Covers configuration validation, the rolling-window statistics helper, each of
the three built-in strategies in isolation, and the full signal-engine pipeline
(cooldowns, sizing, risk caps, intent building, pause/resume, manual close).
No network calls are made; all upstream state is fed directly.
"""

from __future__ import annotations

import os
import sys

# Make the service package importable when running from the service directory.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ste.config import (  # noqa: E402
    MeanReversionParams, MomentumParams, ServiceConfig, SpreadArbParams, validate_config,
)
from ste.models import (  # noqa: E402
    BookView, RollingWindow, SignalSide, SignalStatus, now_ns,
)
from ste.strategies import (  # noqa: E402
    MeanReversionStrategy, MomentumStrategy, SpreadArbStrategy, StrategyContext,
)
from ste.signal_engine import SignalEngine  # noqa: E402


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_default_config_is_valid():
    assert validate_config(ServiceConfig()) == []


def test_invalid_port_rejected():
    cfg = ServiceConfig(listen_port=70_000)
    errs = validate_config(cfg)
    assert any("listen_port" in e for e in errs)


def test_empty_symbol_set_rejected():
    from dataclasses import replace
    from ste.config import IngestConfig
    cfg = ServiceConfig(ingest=replace(IngestConfig(), subscribe_symbols=()))
    assert any("subscribe_symbols" in e for e in validate_config(cfg))


# ---------------------------------------------------------------------------
# RollingWindow
# ---------------------------------------------------------------------------

def test_rolling_window_capacity_and_stats():
    w = RollingWindow(3)
    for p, t in [(10.0, 1), (12.0, 2), (14.0, 3), (16.0, 4)]:
        w.push(p, t)
    assert len(w) == 3
    assert w.oldest().price == 12.0
    assert w.latest().price == 16.0
    assert abs(w.mean() - (12.0 + 14.0 + 16.0) / 3.0) < 1e-9


def test_rolling_window_zscore_flat_is_zero():
    w = RollingWindow(5)
    for i in range(5):
        w.push(100.0, i)
    assert w.zscore(120.0) == 0.0  # zero variance -> no deviation


def test_rolling_window_zscore_direction():
    w = RollingWindow(4)
    for p in [100.0, 100.0, 100.0, 102.0]:
        w.push(p, 0)
    z_up = w.zscore(110.0)
    z_down = w.zscore(90.0)
    assert z_up > 0
    assert z_down < 0


# ---------------------------------------------------------------------------
# Momentum strategy (isolated)
# ---------------------------------------------------------------------------

def _ctx(window, tick=0.25):
    return StrategyContext(canonical_symbol="SYM", venue_id="V", tick_size=tick, mid_window=window)


def test_momentum_enter_on_uptrend():
    w = RollingWindow(8)
    ctx = _ctx(w)
    strat = MomentumStrategy("m", MomentumParams(window_ticks=8, entry_threshold_ticks=3.0))
    # rising mid: 100 -> 102 (8 ticks up over the window)
    prices = [100.0, 100.25, 100.5, 100.75, 101.0, 101.25, 101.5, 102.0]
    for i, p in enumerate(prices):
        ctx.push_mid(p, i)
    dec = strat.on_tick(ctx, None)
    assert dec is not None
    assert dec.action == "ENTER"
    assert dec.side == SignalSide.BUY
    assert dec.suggested_qty >= 1


def test_momentum_no_signal_on_flat():
    w = RollingWindow(8)
    ctx = _ctx(w)
    strat = MomentumStrategy("m", MomentumParams(window_ticks=8, entry_threshold_ticks=3.0))
    for i in range(8):
        ctx.push_mid(100.0 + 0.001 * i, i)
    assert strat.on_tick(ctx, None) is None


def test_momentum_exit_on_reversal():
    w = RollingWindow(8)
    ctx = _ctx(w)
    strat = MomentumStrategy("m", MomentumParams(window_ticks=8, entry_threshold_ticks=3.0,
                                                 exit_threshold_ticks=2.0))
    # long position; mid now falling hard
    ctx.net_position = 5
    prices = [102.0, 101.75, 101.5, 101.25, 101.0, 100.75, 100.5, 100.0]
    for i, p in enumerate(prices):
        ctx.push_mid(p, i)
    dec = strat.on_tick(ctx, None)
    assert dec is not None
    assert dec.action == "EXIT"
    assert dec.side == SignalSide.SELL


# ---------------------------------------------------------------------------
# Mean-reversion strategy (isolated)
# ---------------------------------------------------------------------------

def test_mean_reversion_enter_on_spike():
    w = RollingWindow(16)
    ctx = _ctx(w)
    strat = MeanReversionStrategy("r", MeanReversionParams(window_ticks=16, z_entry_threshold=2.0))
    # mostly flat around 100 with a tight band, then a spike to 104
    base = [100.0, 99.8, 100.2, 99.9, 100.1, 100.0, 99.95, 100.05,
            100.0, 99.9, 100.1, 100.0, 99.98, 100.02, 100.0]
    for i, p in enumerate(base):
        ctx.push_mid(p, i)
    ctx.push_mid(104.0, len(base))
    dec = strat.on_tick(ctx, None)
    assert dec is not None
    assert dec.action == "ENTER"
    assert dec.side == SignalSide.BUY


def test_mean_reversion_no_signal_near_mean():
    w = RollingWindow(16)
    ctx = _ctx(w)
    strat = MeanReversionStrategy("r", MeanReversionParams(window_ticks=16, z_entry_threshold=2.0))
    for i in range(16):
        ctx.push_mid(100.0 + (0.1 if i % 2 else -0.1), i)
    assert strat.on_tick(ctx, None) is None


# ---------------------------------------------------------------------------
# Spread-arbitrage strategy (isolated)
# ---------------------------------------------------------------------------

def _book(mid=100.0, bb=100.0, ba=100.5, bbq=40, baq=10, spread_ticks=2, imb=None):
    if imb is None:
        imb = bbq / max(baq, 1)
    return BookView(
        canonical_symbol="SYM", venue_id="V", best_bid_price=bb, best_bid_qty=bbq,
        best_ask_price=ba, best_ask_qty=baq, mid_price=mid, spread=ba - bb,
        spread_ticks=spread_ticks, imbalance_ratio=imb, tick_size=0.25,
        health="HEALTHY", timestamp_ns=now_ns(),
    )


def test_spread_arb_enter_on_bid_imbalance():
    w = RollingWindow(8)
    ctx = _ctx(w)
    strat = SpreadArbStrategy("a", SpreadArbParams(imbalance_entry_ratio=2.0, spread_max_ticks=3))
    book = _book(bbq=40, baq=10, imb=4.0, spread_ticks=2)
    dec = strat.on_tick(ctx, book)
    assert dec is not None
    assert dec.action == "ENTER"
    assert dec.side == SignalSide.BUY


def test_spread_arb_no_signal_when_too_wide():
    w = RollingWindow(8)
    ctx = _ctx(w)
    strat = SpreadArbStrategy("a", SpreadArbParams(imbalance_entry_ratio=2.0, spread_max_ticks=3))
    book = _book(bbq=40, baq=10, imb=4.0, spread_ticks=5)  # wider than max
    assert strat.on_tick(ctx, book) is None


def test_spread_arb_exit_on_tight_spread():
    w = RollingWindow(8)
    ctx = _ctx(w)
    strat = SpreadArbStrategy("a", SpreadArbParams(imbalance_entry_ratio=2.0,
                                                   spread_max_ticks=3, exit_spread_ticks=1))
    ctx.net_position = 4
    book = _book(bbq=40, baq=10, imb=4.0, spread_ticks=1)
    dec = strat.on_tick(ctx, book)
    assert dec is not None
    assert dec.action == "EXIT"


# ---------------------------------------------------------------------------
# Full engine pipeline
# ---------------------------------------------------------------------------

def _engine():
    eng = SignalEngine(ServiceConfig())
    eng.set_tick_size("SYM", 0.25)
    eng.register_default_strategies()
    return eng


def test_engine_generates_signal_and_intent_on_uptrend():
    eng = _engine()
    prices = [100.0, 100.25, 100.5, 100.75, 101.0, 101.25, 101.5, 102.0]
    signals_seen: list = []
    intents_seen: list = []
    for i, p in enumerate(prices):
        book = BookView(
            canonical_symbol="SYM", venue_id="V", best_bid_price=p - 0.25, best_bid_qty=10,
            best_ask_price=p + 0.25, best_ask_qty=10, mid_price=p, spread=0.5,
            spread_ticks=2, imbalance_ratio=1.0, tick_size=0.25, health="HEALTHY",
            timestamp_ns=i + 1,
        )
        sigs, intents = eng.apply_book_view(book)
        signals_seen.extend(sigs)
        intents_seen.extend(intents)
    assert len(signals_seen) >= 1
    # at least one entry produced an order intent
    assert any(i.side == SignalSide.BUY for i in intents_seen)
    first = signals_seen[0]
    assert first.status == SignalStatus.OPEN
    assert eng.stats["signals_generated"] >= 1


def test_engine_cooldown_suppresses_immediate_repeat():
    eng = _engine()
    book = BookView(
        canonical_symbol="SYM", venue_id="V", best_bid_price=99.75, best_bid_qty=10,
        best_ask_price=100.25, best_ask_qty=10, mid_price=100.0, spread=0.5,
        spread_ticks=2, imbalance_ratio=1.0, tick_size=0.25, health="HEALTHY",
        timestamp_ns=1,
    )
    # build an uptrend to fire a signal
    prices = [100.0, 100.25, 100.5, 100.75, 101.0, 101.25, 101.5, 102.0]
    fired = False
    for i, p in enumerate(prices):
        b = BookView(
            canonical_symbol="SYM", venue_id="V", best_bid_price=p - 0.25, best_bid_qty=10,
            best_ask_price=p + 0.25, best_ask_qty=10, mid_price=p, spread=0.5,
            spread_ticks=2, imbalance_ratio=1.0, tick_size=0.25, health="HEALTHY",
            timestamp_ns=i + 1,
        )
        sigs, _ = eng.apply_book_view(b)
        if sigs:
            fired = True
    assert fired
    # immediately re-feed the same final book; cooldown should suppress new signals
    b2 = BookView(
        canonical_symbol="SYM", venue_id="V", best_bid_price=101.75, best_bid_qty=10,
        best_ask_price=102.25, best_ask_qty=10, mid_price=102.0, spread=0.5,
        spread_ticks=2, imbalance_ratio=1.0, tick_size=0.25, health="HEALTHY",
        timestamp_ns=2,
    )
    sigs2, _ = eng.apply_book_view(b2)
    assert len(sigs2) == 0
    assert eng.stats["cooldown_skips"] >= 1


def test_engine_intent_notional_cap():
    from dataclasses import replace
    from ste.config import EmitConfig
    cfg = ServiceConfig(emit=replace(EmitConfig(), max_notional_per_signal=5_000.0))
    eng = SignalEngine(cfg)
    eng.set_tick_size("SYM", 0.25)
    eng.register_default_strategies()
    # a strong uptrend; price ~100 so a large qty would blow the 5k notional cap
    prices = [100.0, 100.25, 100.5, 100.75, 101.0, 101.25, 101.5, 102.0]
    intents_seen: list = []
    for i, p in enumerate(prices):
        book = BookView(
            canonical_symbol="SYM", venue_id="V", best_bid_price=p - 0.25, best_bid_qty=10,
            best_ask_price=p + 0.25, best_ask_qty=10, mid_price=p, spread=0.5,
            spread_ticks=2, imbalance_ratio=1.0, tick_size=0.25, health="HEALTHY",
            timestamp_ns=i + 1,
        )
        _, intents = eng.apply_book_view(book)
        intents_seen.extend(intents)
    assert intents_seen
    for intent in intents_seen:
        assert intent.notional <= 5_000.0 + 1e-6


def test_engine_pause_resume():
    eng = _engine()
    sid = "mom-default"
    assert eng.pause_strategy(sid) is True
    assert eng.strategy_states()[sid] == "PAUSED"
    prices = [100.0, 100.25, 100.5, 100.75, 101.0, 101.25, 101.5, 102.0]
    for i, p in enumerate(prices):
        book = BookView(
            canonical_symbol="SYM", venue_id="V", best_bid_price=p - 0.25, best_bid_qty=10,
            best_ask_price=p + 0.25, best_ask_qty=10, mid_price=p, spread=0.5,
            spread_ticks=2, imbalance_ratio=1.0, tick_size=0.25, health="HEALTHY",
            timestamp_ns=i + 1,
        )
        eng.apply_book_view(book)
    # momentum paused -> no momentum signals (mean-reversion stays flat too here)
    assert eng.stats["signals_generated"] == 0
    assert eng.resume_strategy(sid) is True
    assert eng.strategy_states()[sid] == "ACTIVE"


def test_engine_manual_close_flattens_position():
    eng = _engine()
    prices = [100.0, 100.25, 100.5, 100.75, 101.0, 101.25, 101.5, 102.0]
    opened_id = None
    for i, p in enumerate(prices):
        book = BookView(
            canonical_symbol="SYM", venue_id="V", best_bid_price=p - 0.25, best_bid_qty=10,
            best_ask_price=p + 0.25, best_ask_qty=10, mid_price=p, spread=0.5,
            spread_ticks=2, imbalance_ratio=1.0, tick_size=0.25, health="HEALTHY",
            timestamp_ns=i + 1,
        )
        sigs, _ = eng.apply_book_view(book)
        if sigs:
            opened_id = sigs[0].id
    assert opened_id is not None
    closed = eng.close_signal(opened_id)
    assert closed is not None
    assert closed.status == SignalStatus.CLOSED
    for c in eng.all_contexts():
        if c.canonical_symbol == "SYM":
            assert c.net_position == 0


def test_engine_quote_fallback_seeds_window():
    eng = _engine()
    # feed raw quotes (no book) — should process ticks without error
    for i in range(10):
        quote = {"sym": "SYM", "ven": "V", "px": 100.0 + 0.25 * i, "rt": i + 1}
        eng.apply_quote(quote)
    assert eng.stats["ticks_processed"] == 10
