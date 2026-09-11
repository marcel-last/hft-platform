"""M2 tests: per-source dedup logic (PLAN §3.3) against a fake Transport.

No sockets: :class:`FakeTransport` serves canned JSON documents per
(path, call-count), so each source's cursor / dedup state is exercised
deterministically.
"""

from typing import Any, Dict, List, Tuple

from dash.live_sources import (
    AltsvcSource,
    AudlSource,
    CfgsSource,
    LatmonSource,
    MdgSource,
    ObbSource,
    SourceTransportError,
)


class FakeTransport:
    """Routes (path) -> callable(call_index) -> doc.  Raises on unknown."""

    def __init__(self, routes: Dict[str, Any]) -> None:
        self.routes = routes
        self.calls: Dict[str, List[str]] = {}
        self.path_log: List[str] = []

    def get(self, base_url: str, path: str, timeout_s: float) -> Any:
        self.calls.setdefault(path, []).append(path)
        self.path_log.append(path)
        n = len(self.calls[path]) - 1
        spec = self.routes[path]
        if callable(spec):
            return spec(n)
        return spec


T = (1.5,)  # timeout is never used by the fake; keep call sites readable


def _s1(n: int) -> Dict[str, Any]:
    return {"canonical_symbol": "SYM%d" % (n + 1), "tick_size": 0.5}


def test_mdg_default_symbols_first_three_from_s1_symbols():
    t = FakeTransport({
        "/symbols": {"symbols": [_s1(0), _s1(1), _s1(2), _s1(3)]},
        "/quotes/SYM1?limit=1": {"symbol": "SYM1", "count": 1,
                                 "quotes": [{"seq": 5, "px": 1.0}]},
        "/quotes/SYM2?limit=1": {"symbol": "SYM2", "count": 1,
                                 "quotes": [{"seq": 9, "px": 2.0}]},
        "/quotes/SYM3?limit=1": {"symbol": "SYM3", "count": 0, "quotes": []},
    })
    src = MdgSource(t, "http://h:7610", *T)
    out = src.poll_once()
    assert [q["seq"] for q in out] == [5, 9]
    # second poll: only seq advances count as new
    t.routes["/quotes/SYM1?limit=1"] = {"quotes": [{"seq": 6, "px": 1.1}]}
    t.routes["/quotes/SYM2?limit=1"] = {"quotes": [{"seq": 9, "px": 2.0}]}
    out = src.poll_once()
    assert [q["seq"] for q in out] == [6]


def test_mdg_explicit_symbols_skip_symbols_lookup_and_dedup_by_seq():
    t = FakeTransport({
        "/quotes/ES1?limit=1": lambda n: (
            {"quotes": [{"seq": 100, "px": 1}]} if n == 0
            else {"quotes": [{"seq": 99, "px": 1}]}),
        "/quotes/ES2?limit=1": lambda n: (
            {"quotes": [{"seq": 101, "px": 2}]} if n == 0
            else {"quotes": [{"seq": 102, "px": 2}]}),
    })
    src = MdgSource(t, "http://h:7610", *T, symbols=["ES1", "ES2"])
    first = src.poll_once()
    assert [q["seq"] for q in first] == [100, 101]
    second = src.poll_once()
    # seq 99 < 100 for ES1 (no emit); 102 > 101 for ES2 (emit)
    assert [q["seq"] for q in second] == [102]
    assert not any(p.startswith("/symbols") for p in t.path_log)


def test_mdg_no_symbols_raises_transport_error_once():
    t = FakeTransport({"/symbols": {"symbols": []}})
    src = MdgSource(t, "http://h:7610", *T)
    try:
        src.poll_once()
    except SourceTransportError as e:
        assert "no symbols" in e.reason
    else:
        raise AssertionError("expected SourceTransportError")


def test_obb_dedups_on_ts_cursor():
    t = FakeTransport({
        "/events?limit=100": lambda n: {
            "count": 2,
            "events": [
                {"kind": "TOP_CHANGE", "ts": 10 * n + 10},
                {"kind": "SPREAD_CHANGE", "ts": 10 * n + 11},
            ],
        },
    })
    src = ObbSource(t, "http://h:7620", *T)
    out1 = src.poll_once()
    assert [e["ts"] for e in out1] == [10, 11]
    out2 = src.poll_once()
    assert [e["ts"] for e in out2] == [20, 21]
    # the tail of the previous window plus one new event: only the new one emits
    t.routes["/events?limit=100"] = {
        "events": [{"ts": 21}, {"ts": 22}], "count": 2}
    out3 = src.poll_once()
    assert [e["ts"] for e in out3] == [22]
    # older ts entries (replayed window) never re-emit
    t.routes["/events?limit=100"] = {
        "events": [{"ts": 10}, {"ts": 23}], "count": 2}
    out4 = src.poll_once()
    assert [e["ts"] for e in out4] == [23]


def test_latmon_emits_only_transitions():
    stages = lambda b1, b2: {  # noqa: E731
        "stages": [
            {"stage": "mdg->obb", "breached": b1, "p99_ns": 1},
            {"stage": "obb->ste", "breached": b2, "p99_ns": 2},
        ],
    }
    t = FakeTransport({
        "/latency": lambda n: (
            stages(False, False) if n == 0
            else stages(True, False) if n == 1
            else stages(True, False) if n == 2
            else stages(False, False)),
    })
    src = LatmonSource(t, "http://h:7670", *T)
    assert src.poll_once() == []          # baseline: no history yet
    out = src.poll_once()                 # mdg->obb -> breach
    assert len(out) == 1
    assert out[0]["state"] == "breach"
    assert out[0]["stage"] == "mdg->obb"
    assert src.poll_once() == []          # steady-state: silent
    out = src.poll_once()                 # mdg->obb -> recovery
    assert len(out) == 1
    assert out[0]["state"] == "recovery"


def test_altsvc_dedups_on_alert_id_and_tolerates_replay():
    t = FakeTransport({
        "/alerts/dispatch?limit=100": lambda n: {
            "count": 2,
            "dispatches": [
                {"alert_id": "ALT-0007", "ts": 1},
                {"alert_id": "ALT-0008", "ts": 2},
            ],
        },
    })
    src = AltsvcSource(t, "http://h:7700", *T)
    out1 = src.poll_once()
    assert [d["alert_id"] for d in out1] == ["ALT-0007", "ALT-0008"]
    assert src.poll_once() == []  # same log: nothing new
    t.routes["/alerts/dispatch?limit=100"] = {
        "count": 3,
        "dispatches": [
            {"alert_id": "ALT-0009", "ts": 3},
            {"alert_id": "ALT-0008", "ts": 4},  # escalated: new log entry,
            {"alert_id": "ALT-0007", "ts": 5},  # old id -> must NOT re-emit
        ],
    }
    out2 = src.poll_once()
    assert [d["alert_id"] for d in out2] == ["ALT-0009"]


def test_altsvc_seen_set_is_bounded():
    t = FakeTransport({"/alerts/dispatch?limit=100":
                       {"dispatches": [{"alert_id": "ALT-%04d" % i, "ts": i}
                                       for i in range(200)]}})
    src = AltsvcSource(t, "http://h:7700", *T)
    src.poll_once()
    assert len(src._seen) <= AltsvcSource.MAX_SEEN


def test_cfgs_long_poll_emits_each_change_once_and_advances_since():
    t = FakeTransport({
        "/changes/watch?timeout_ms=4000&since=0":
            {"changed": True, "latest_seq": 3,
             "events": [{"seq": 1}, {"seq": 2}, {"seq": 3}]},
        # The since=3 watch times out once, then a change arrives while
        # still waiting on the same cursor (the real long-poll blocks for
        # up to timeout_ms before answering with whatever has changed).
        "/changes/watch?timeout_ms=4000&since=3": lambda n: (
            {"changed": False, "latest_seq": 3, "events": []} if n == 0
            else {"changed": True, "latest_seq": 5,
                  "events": [{"seq": 4}, {"seq": 5}]}),
    })
    src = CfgsSource(t, "http://h:7710", *T, watch_timeout_ms=4000)
    out1 = src.poll_once()
    assert [e["seq"] for e in out1] == [1, 2, 3]
    out2 = src.poll_once()   # cursor now 3 -> long-poll timeout, empty
    assert out2 == []
    out3 = src.poll_once()   # change at seq 4..5 on the same since=3 watch
    assert [e["seq"] for e in out3] == [4, 5]
    assert src._since == 5
    t.routes["/changes/watch?timeout_ms=4000&since=5"] = {
        "changed": False, "latest_seq": 5, "events": []}
    out4 = src.poll_once()   # cursor now 5: a fresh (empty) watch
    assert out4 == []


def test_audl_dedups_on_event_id_hex_seq():
    eid = lambda i: "EVT-%012x" % i
    t = FakeTransport({
        "/events?limit=100": {
            "count": 2,
            "events": [
                {"event_id": eid(0xa), "kind": "auth.login"},
                {"event_id": eid(0xf), "kind": "alert.submit"},
            ],
        },
    })
    src = AudlSource(t, "http://h:7730", *T)
    out1 = src.poll_once()
    assert [e["event_id"] for e in out1] == [eid(0xa), eid(0xf)]
    # The next window replays 0xf and adds 0x10.  The hex digits make
    # 0x10 == 16 > 15; a lexicographic string compare would rank
    # "...10" *below* "...0f" and drop it.  Only the new one emits.
    t.routes["/events?limit=100"] = {"events": [
        {"event_id": eid(0xf)}, {"event_id": eid(0x10)}]}
    out2 = src.poll_once()
    assert [e["event_id"] for e in out2] == [eid(0x10)]
    assert AudlSource._seq_of("nope") is None
    assert AudlSource._seq_of(eid(5)) == 5


def test_bad_json_doc_raises_source_transport_error():
    class Bad:
        def get(self, base_url, path, timeout_s):
            return [1, 2, 3]
    src = ObbSource(Bad(), "http://h:7620", *T)
    try:
        src.poll_once()
    except SourceTransportError as e:
        assert "non-object" in e.reason
    else:
        raise AssertionError("expected SourceTransportError")
