"""market_data_gateway — unit tests for the normalization pipeline."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdg.config import CONFIG, validate_config
from mdg.errors import UnknownSymbolError
from mdg.models import QuoteAction, RawQuote, Side, now_ns
from mdg.normalizer import QuoteNormalizer


def test_config_validates_clean():
    assert validate_config(CONFIG) == []


def test_symbol_mapping_roundtrip():
    n = QuoteNormalizer()
    assert n.map_symbol("EUREX", "FESX") == "EU_STOXX50_CONT"
    assert n.map_symbol("CME-GLOBEX", "ES") == "US_S&P500_CONT"


def test_unknown_symbol_raises():
    n = QuoteNormalizer()
    try:
        n.map_symbol("NOPE", "ZZZ")
        raise AssertionError("expected UnknownSymbolError")
    except UnknownSymbolError:
        pass


def test_normalize_quote_on_grid():
    n = QuoteNormalizer()
    raw = RawQuote(
        venue_id="EUREX", symbol_venue="FESX", seq_no=1,
        msg_type=QuoteAction.NEW, side=Side.BID,
        price=5000.0, quantity=10, depth_level=1,
        venue_timestamp_ns=now_ns(), receive_timestamp_ns=now_ns(),
    )
    q = n.normalize_quote(raw)
    assert q is not None
    assert q.canonical_symbol == "EU_STOXX50_CONT"
    assert q.tick_size == 1.0
    assert q.quality.value in ("FRESH", "STALE_WARN")


def test_normalize_unknown_symbol_drops():
    n = QuoteNormalizer()
    raw = RawQuote(
        venue_id="EUREX", symbol_venue="BOGUS", seq_no=1,
        msg_type=QuoteAction.NEW, side=Side.BID,
        price=1.0, quantity=1, depth_level=1,
        venue_timestamp_ns=now_ns(), receive_timestamp_ns=now_ns(),
    )
    assert n.normalize_quote(raw) is None
    assert n.stats["unknown_symbols_dropped"] == 1


def test_sequence_gap_detection():
    from mdg.models import SequenceTracker
    t = SequenceTracker(venue_id="V", symbol_venue="S")
    now = now_ns()
    assert t.observe(1, now) is None
    assert t.observe(2, now) is None
    gap = t.observe(5, now)          # skipped 3 and 4
    assert gap == 2
    assert t.total_gaps == 1
