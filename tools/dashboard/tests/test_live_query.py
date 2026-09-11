"""M2 tests: ``parse_live_query`` parameter validation (PLAN §3.5).

Invalid ``sources`` / ``max_age_s`` values must raise ``UI-202`` **before**
the stream opens; valid and empty input must produce the documented
defaults.
"""

from dash.errors import UnsupportedParamError
from dash.live import LIVE_SOURCES, LiveConfig, parse_live_query


def test_default_is_all_six_sources_in_plan_order():
    req = parse_live_query({}, LiveConfig())
    assert req.sources == ["mdg", "obb", "latmon", "altsvc", "cfgs", "audl"]
    assert req.symbols == []
    assert req.max_age_s == 3600.0


def test_subset_preserves_plan_order_and_dedups():
    req = parse_live_query({"sources": ["audl,mdg", "mdg"]}, LiveConfig())
    assert req.sources == ["mdg", "audl"]


def test_empty_sources_value_falls_back_to_all():
    req = parse_live_query({"sources": [""]}, LiveConfig())
    assert req.sources == list(LIVE_SOURCES)


def test_bad_sources_value_raises_ui202():
    try:
        parse_live_query({"sources": ["mdg,zzz"]}, LiveConfig())
    except UnsupportedParamError as e:
        assert e.code == "UI-202"
        assert e.status == 400
        assert e.context.get("value") == "zzz"
    else:
        raise AssertionError("expected UnsupportedParamError")


def test_repeatable_symbol_keeps_order_and_dedups():
    req = parse_live_query({"symbol": ["EU_STOXX50_CONT", "ES1",
                                       "EU_STOXX50_CONT", "  ES1 "]},
                            LiveConfig())
    assert req.symbols == ["EU_STOXX50_CONT", "ES1"]


def test_symbols_ignored_for_non_mdg_but_still_parsed():
    req = parse_live_query({"sources": ["audl"],
                            "symbol": ["ES1"]}, LiveConfig())
    assert req.sources == ["audl"]
    assert req.symbols == ["ES1"]


def test_max_age_s_numeric_override():
    req = parse_live_query({"max_age_s": ["12"]}, LiveConfig())
    assert req.max_age_s == 12.0


def test_max_age_s_bad_value_raises_ui202():
    for bad in ["abc", "-5", "0"]:
        try:
            parse_live_query({"max_age_s": [bad]}, LiveConfig())
        except UnsupportedParamError as e:
            assert e.code == "UI-202"
            assert e.context.get("value") == bad
        else:
            raise AssertionError("expected UnsupportedParamError for %r" % bad)
