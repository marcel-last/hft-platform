"""data_quality_monitor — unit tests (stdlib + pytest-compatible, no network).

Exercises the config validation, model parsing, composite scoring + hysteresis,
degradation-episode recording, read views, error envelopes, and the router.
All upstream data is injected directly into the engine (no HTTP).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the src/ package importable when running tests from the service dir.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dqm.config import (  # noqa: E402
    AlertingConfig,
    IngestConfig,
    ScoringConfig,
    ServiceConfig,
    ThresholdsConfig,
    validate_config,
)
from dqm.errors import (  # noqa: E402
    DQMErrors,
    UnknownSymbolError,
    UpstreamUnreachableError,
    error_envelope,
)
from dqm.models import (  # noqa: E402
    BookHealthRow,
    DegradationKind,
    FeedMetrics,
    QualityState,
    Severity,
    next_degradation_id,
    now_ns,
)
from dqm.quality_engine import QualityEngine, severity_for_score  # noqa: E402
from dqm.router import build_router  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_engine() -> QualityEngine:
    return QualityEngine(ServiceConfig())


def feed_report(symbols) -> dict:
    """Build an S1 /quality-shaped report from a {symbol: metrics-dict} map."""
    return {"symbols": symbols, "staleness_breaches_total": 0, "symbols_tracked": len(symbols)}


def healthy_feed(symbol: str, **kw) -> dict:
    base = {
        "quotes_seen": 1000,
        "stale_pct": 0.0,
        "gap_events": 0,
        "out_of_order_events": 0,
        "max_staleness_ms_observed": 5.0,
        "silent_ms": 20.0,
    }
    base.update(kw)
    return base


def book_row(symbol: str, venue: str = "EUREX", health: str = "HEALTHY",
             cross_events: int = 0) -> dict:
    return {
        "symbol": symbol,
        "venue": venue,
        "health": health,
        "cross_events": cross_events,
        "messages_applied": 500,
        "rebuilds": 0,
    }


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_validate_config_default_is_valid():
    assert validate_config(ServiceConfig()) == []


def test_validate_config_bad_port():
    cfg = ServiceConfig(listen_port=70000)
    errs = validate_config(cfg)
    assert any("listen_port" in e for e in errs)


def test_validate_config_bad_urls():
    cfg = ServiceConfig(ingest=IngestConfig(market_data_gateway_url="ftp://nope"))
    errs = validate_config(cfg)
    assert any("market_data_gateway_url" in e for e in errs)


def test_validate_config_threshold_ordering():
    cfg = ServiceConfig(thresholds=ThresholdsConfig(stale_pct_warn=50.0, stale_pct_crit=10.0))
    errs = validate_config(cfg)
    assert any("stale_pct_warn must be <=" in e for e in errs)


def test_validate_config_hysteresis_band():
    # degraded_below must be strictly less than recovered_at_or_above
    cfg = ServiceConfig(scoring=ScoringConfig(degraded_below=90, recovered_at_or_above=85))
    errs = validate_config(cfg)
    assert any("hysteresis band" in e for e in errs)


def test_validate_config_zero_poll_interval():
    cfg = ServiceConfig(ingest=IngestConfig(poll_interval_ms=0))
    errs = validate_config(cfg)
    assert any("poll_interval_ms" in e for e in errs)


# ---------------------------------------------------------------------------
# Model parsing
# ---------------------------------------------------------------------------

def test_feed_metrics_from_dict_defaults():
    fm = FeedMetrics.from_dict("SYM", {})
    assert fm.symbol == "SYM"
    assert fm.quotes_seen == 0
    assert fm.stale_pct == 0.0
    assert fm.silent_ms == 0.0


def test_feed_metrics_from_dict_values():
    fm = FeedMetrics.from_dict(
        "SYM",
        {"quotes_seen": 42, "stale_pct": 12.5, "gap_events": 3,
         "out_of_order_events": 1, "max_staleness_ms_observed": 800.0, "silent_ms": 1500.0},
    )
    assert fm.quotes_seen == 42
    assert fm.stale_pct == 12.5
    assert fm.gap_events == 3
    assert fm.out_of_order_events == 1
    assert fm.max_staleness_ms_observed == 800.0
    assert fm.silent_ms == 1500.0


def test_book_health_row_from_dict():
    row = BookHealthRow.from_dict(
        {"symbol": "SYM", "venue": "EUREX", "health": "STALE",
         "cross_events": 4, "messages_applied": 10, "rebuilds": 2}
    )
    assert row.symbol == "SYM"
    assert row.venue == "EUREX"
    assert row.health == "STALE"
    assert row.cross_events == 4
    assert row.rebuilds == 2


def test_degradation_id_is_sequential_and_unique():
    a = next_degradation_id()
    b = next_degradation_id()
    assert a != b
    assert a.startswith("DGR-")
    assert len(a) == len(b)


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

def test_severity_for_score_boundaries():
    assert severity_for_score(39) is Severity.CRITICAL
    assert severity_for_score(40) is Severity.MAJOR
    assert severity_for_score(59) is Severity.MAJOR
    assert severity_for_score(60) is Severity.MINOR
    assert severity_for_score(100) is Severity.MINOR


# ---------------------------------------------------------------------------
# Scoring + hysteresis (the core)
# ---------------------------------------------------------------------------

def test_healthy_symbol_scores_ok_with_no_degradation():
    eng = make_engine()
    events = eng.ingest_and_score(
        feed_report({"SYM": healthy_feed("SYM")}),
        [BookHealthRow.from_dict(book_row("SYM"))],
    )
    assert events == []
    sq = eng.symbol_view("SYM")
    assert sq is not None
    assert sq.state is QualityState.OK
    assert sq.score == 100


def test_silent_feed_degrades_symbol():
    eng = make_engine()
    # silent_ms=5000 > max_silent_ms(1000) => feed gap penalty (4*10=40) -> score 60
    events = eng.ingest_and_score(
        feed_report({"SYM": healthy_feed("SYM", silent_ms=5000.0)}),
        [],
    )
    assert len(events) == 1
    assert events[0].kind is DegradationKind.DEGRADED
    sq = eng.symbol_view("SYM")
    assert sq.state is QualityState.DEGRADED
    assert sq.score == 60
    assert any("feed silent" in r for r in sq.reasons)


def test_stale_pct_penalizes_score():
    eng = make_engine()
    # stale_pct=50 > crit(25): penalty=(50-5)*2=90 -> score 10 (CRITICAL)
    events = eng.ingest_and_score(
        feed_report({"SYM": healthy_feed("SYM", stale_pct=50.0)}),
        [],
    )
    sq = eng.symbol_view("SYM")
    assert sq.score == 10
    assert any(events[0].severity is Severity.CRITICAL for events in [events])


def test_stale_book_venue_penalizes():
    eng = make_engine()
    # stale book on one venue => +15 -> score 85 (still OK, not below 70)
    events = eng.ingest_and_score(
        feed_report({"SYM": healthy_feed("SYM")}),
        [BookHealthRow.from_dict(book_row("SYM", "EUREX", health="STALE"))],
    )
    assert events == []
    sq = eng.symbol_view("SYM")
    assert sq.score == 85
    assert sq.state is QualityState.OK
    assert sq.book_stale_venues == ["EUREX"]


def test_cross_events_over_budget_penalize():
    eng = make_engine()
    # cross_events=10 > budget(3): penalty=(10-3)*2=14 -> score 86 (OK)
    events = eng.ingest_and_score(
        feed_report({"SYM": healthy_feed("SYM")}),
        [BookHealthRow.from_dict(book_row("SYM", cross_events=10))],
    )
    assert events == []
    sq = eng.symbol_view("SYM")
    assert sq.score == 86
    assert any("cross events" in r for r in sq.reasons)


def test_combined_penalties_clamp_at_zero():
    eng = make_engine()
    # silence(40) + stale_pct crit(90) => penalty >=130 -> clamped to score 0
    events = eng.ingest_and_score(
        feed_report({"SYM": healthy_feed("SYM", silent_ms=9000.0, stale_pct=80.0)}),
        [],
    )
    sq = eng.symbol_view("SYM")
    assert sq.score == 0
    assert events[0].severity is Severity.CRITICAL


def test_hysteresis_does_not_flap():
    eng = make_engine()
    # 1) degrade: silent feed -> score 60 (<70) => DEGRADED
    eng.ingest_and_score(feed_report({"SYM": healthy_feed("SYM", silent_ms=5000.0)}), [])
    assert eng.symbol_view("SYM").state is QualityState.DEGRADED

    # 2) partial recovery: worst quote age 1400ms -> penalty (900//50)+2 = 20
    #    -> score 80, which is below recovered_at_or_above(85) => stays DEGRADED
    events = eng.ingest_and_score(
        feed_report({"SYM": healthy_feed("SYM", max_staleness_ms_observed=1400.0)}), []
    )
    assert eng.symbol_view("SYM").score == 80
    assert eng.symbol_view("SYM").state is QualityState.DEGRADED

    # 3) now a stale book brings score to exactly 85 (>=85) => recovers to OK
    events = eng.ingest_and_score(
        feed_report({"SYM": healthy_feed("SYM")}),
        [BookHealthRow.from_dict(book_row("SYM", health="STALE"))],
    )
    sq = eng.symbol_view("SYM")
    assert sq.score == 85
    assert sq.state is QualityState.OK


def test_recovery_event_recorded():
    eng = make_engine()
    eng.ingest_and_score(feed_report({"SYM": healthy_feed("SYM", silent_ms=5000.0)}), [])
    # recover fully to score 100 (>=85) => RECOVERED episode
    eng.ingest_and_score(feed_report({"SYM": healthy_feed("SYM")}), [])
    degs = eng.degradations()
    kinds = [d["kind"] for d in degs]
    assert "DEGRADED" in kinds
    assert "RECOVERED" in kinds


def test_empty_upstreams_do_not_flap_state():
    eng = make_engine()
    # first establish OK
    eng.ingest_and_score(feed_report({"SYM": healthy_feed("SYM")}), [])
    # then both upstreams down -> no scoring, state preserved as OK
    events = eng.ingest_and_score(None, [])
    assert events == []
    assert eng.symbol_view("SYM").state is QualityState.OK


def test_union_of_symbols_from_both_upstreams():
    eng = make_engine()
    # S1 knows A; S2 knows B (book only)
    events = eng.ingest_and_score(
        feed_report({"A": healthy_feed("A")}),
        [BookHealthRow.from_dict(book_row("B"))],
    )
    assert eng.symbol_view("A") is not None
    b = eng.symbol_view("B")
    assert b is not None
    # B has no feed metrics: silent_ms=0, stale_pct=0 -> healthy book => OK score 100
    assert b.score == 100
    assert b.state is QualityState.OK


# ---------------------------------------------------------------------------
# Read views
# ---------------------------------------------------------------------------

def test_summary_shape_and_counts():
    eng = make_engine()
    eng.ingest_and_score(
        feed_report({"OK_SYM": healthy_feed("OK_SYM"),
                     "BAD_SYM": healthy_feed("BAD_SYM", silent_ms=5000.0)}),
        [],
    )
    summary = eng.summary()
    assert summary["symbols_total"] == 2
    assert summary["counts"]["OK"] == 1
    assert summary["counts"]["DEGRADED"] == 1
    assert "BAD_SYM" in summary["degraded_symbols"]
    # degraded symbols sort first
    assert summary["symbols"][0]["symbol"] == "BAD_SYM"


def test_gaps_lists_silence_and_sequence_gaps():
    eng = make_engine()
    eng.ingest_and_score(
        feed_report({
            "SILENT": healthy_feed("SILENT", silent_ms=4000.0),
            "GAPPED": healthy_feed("GAPPED", gap_events=7),
        }),
        [],
    )
    gaps = eng.gaps()
    kinds = {(g.symbol, g.kind) for g in gaps}
    assert ("SILENT", "FEED_SILENCE") in kinds
    assert ("GAPPED", "SEQUENCE_GAP") in kinds
    # FEED_SILENCE sorts before SEQUENCE_GAP (most severe first)
    assert gaps[0].kind == "FEED_SILENCE"


def test_staleness_view_sorted_by_worst_age():
    eng = make_engine()
    eng.ingest_and_score(
        feed_report({
            "FRESH": healthy_feed("FRESH", max_staleness_ms_observed=10.0),
            "STALE": healthy_feed("STALE", max_staleness_ms_observed=900.0),
        }),
        [],
    )
    samples = eng.staleness()
    assert samples[0].symbol == "STALE"
    assert samples[0].max_staleness_ms == 900.0


def test_degradations_filter_by_kind():
    eng = make_engine()
    eng.ingest_and_score(feed_report({"SYM": healthy_feed("SYM", silent_ms=5000.0)}), [])
    eng.ingest_and_score(feed_report({"SYM": healthy_feed("SYM")}), [])  # recover
    all_events = eng.degradations()
    only_degraded = eng.degradations(kind="DEGRADED")
    assert len(all_events) == 2
    assert len(only_degraded) == 1
    assert only_degraded[0]["kind"] == "DEGRADED"


def test_degradation_history_is_bounded():
    cfg = ServiceConfig(alerting=AlertingConfig(max_degradations=5))
    eng = QualityEngine(cfg)
    # force 8 degradation episodes by toggling a symbol between silent and healthy
    for i in range(8):
        if i % 2 == 0:
            eng.ingest_and_score(feed_report({"SYM": healthy_feed("SYM", silent_ms=5000.0)}), [])
        else:
            eng.ingest_and_score(feed_report({"SYM": healthy_feed("SYM")}), [])
    assert len(eng.degradations(limit=1000)) <= 5


def test_symbol_view_returns_copy_not_live_reference():
    eng = make_engine()
    eng.ingest_and_score(feed_report({"SYM": healthy_feed("SYM")}), [])
    a = eng.symbol_view("SYM")
    b = eng.symbol_view("SYM")
    a.reasons.append("mutated")
    assert "mutated" not in b.reasons


def test_stats_view_counts():
    eng = make_engine()
    eng.ingest_and_score(
        feed_report({"A": healthy_feed("A"), "B": healthy_feed("B", silent_ms=5000.0)}),
        [],
    )
    stats = eng.stats_view()
    assert stats["symbols_tracked"] == 2
    assert stats["state_counts"]["DEGRADED"] == 1
    assert stats["passes_completed"] == 1
    assert stats["degradations_total"] >= 1


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------

def test_error_envelope_known_error():
    env = error_envelope(UnknownSymbolError("NOPE"))
    err = env["error"]
    assert err["code"] == "DQM-203"
    assert err["service"] == "data-quality-monitor"
    assert err["retryable"] is False
    assert err["context"]["symbol"] == "NOPE"


def test_error_envelope_upstream_retryable():
    env = error_envelope(UpstreamUnreachableError("market-data-gateway", "timeout"))
    err = env["error"]
    assert err["code"] == "DQM-301"
    assert err["retryable"] is True
    assert err["context"]["target"] == "market-data-gateway"


def test_error_envelope_unknown_exception():
    env = error_envelope(ValueError("boom"))
    err = env["error"]
    assert err["code"] == "DQM-999"
    assert err["retryable"] is False
    assert err["context"]["exception_type"] == "ValueError"


# ---------------------------------------------------------------------------
# Router dispatch (no HTTP, direct dispatch)
# ---------------------------------------------------------------------------

class _FakeController:
    def __init__(self):
        self.engine = make_engine()
        self.engine.ingest_and_score(
            feed_report({"SYM": healthy_feed("SYM", silent_ms=5000.0)}),
            [],
        )

    # thin pass-throughs matching the real controller surface
    def healthz(self):
        from dqm.config import CONFIG
        return 200, {"status": "ok", "service": CONFIG.name, "version": CONFIG.version}

    def readyz(self):
        reasons = list(self.engine.readiness_reasons())
        return (200 if not reasons else 503), {"status": "ready" if not reasons else "not_ready", "reasons": reasons}

    def quality_summary(self):
        return 200, self.engine.summary()

    def quality_symbol(self, symbol):
        from dqm.errors import UnknownSymbolError, error_envelope
        sq = self.engine.symbol_view(symbol)
        if sq is None:
            return 404, error_envelope(UnknownSymbolError(symbol))
        return 200, {"quality": sq.to_dict()}

    def gaps(self, limit=100):
        recs = self.engine.gaps(limit=limit)
        return 200, {"count": len(recs), "gaps": [r.to_dict() for r in recs]}

    def staleness(self, limit=100):
        samples = self.engine.staleness(limit=limit)
        return 200, {"count": len(samples), "staleness": [s.to_dict() for s in samples]}

    def degradations(self, limit=100, kind=None):
        events = self.engine.degradations(limit=limit, kind=kind)
        return 200, {"count": len(events), "degradations": events}

    def stats(self):
        return 200, {"engine": self.engine.stats_view()}


def test_router_healthz():
    router = build_router(_FakeController())
    status, body = router.dispatch("GET", "/healthz", {})
    assert status == 200
    assert body["status"] == "ok"
    assert body["service"] == "data-quality-monitor"


def test_router_quality_symbol_found():
    router = build_router(_FakeController())
    status, body = router.dispatch("GET", "/quality/SYM", {})
    assert status == 200
    assert body["quality"]["symbol"] == "SYM"
    assert body["quality"]["state"] == "DEGRADED"


def test_router_quality_symbol_not_found():
    router = build_router(_FakeController())
    status, body = router.dispatch("GET", "/quality/NOPE", {})
    assert status == 404
    assert body["error"]["code"] == "DQM-203"


def test_router_gaps_with_query_limit():
    router = build_router(_FakeController())
    status, body = router.dispatch("GET", "/gaps", {"limit": "1"})
    assert status == 200
    assert body["count"] <= 1


def test_router_degradations_kind_filter():
    router = build_router(_FakeController())
    status, body = router.dispatch("GET", "/degradations", {"kind": "DEGRADED"})
    assert status == 200
    assert all(d["kind"] == "DEGRADED" for d in body["degradations"])


def test_router_unknown_route_envelope():
    router = build_router(_FakeController())
    status, body = router.dispatch("GET", "/does-not-exist", {})
    assert status == 404
    assert body["error"]["code"] == "DQM-404"
    assert body["error"]["service"] == "data-quality-monitor"


def test_router_method_mismatch_is_404():
    router = build_router(_FakeController())
    status, body = router.dispatch("POST", "/healthz", {})
    assert status == 404
    assert body["error"]["code"] == "DQM-404"
