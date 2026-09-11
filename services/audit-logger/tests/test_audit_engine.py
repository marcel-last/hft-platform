"""S13 audit-logger unit tests: config, canonical hashing, ids, chain, dedup,
eviction, verification, stats.

Run: ``cd services/audit-logger && python -m pytest tests/ -q``
No network, no sleeps — all time behaviour via ``ManualClock`` (CONVENTIONS §10).
"""

import dataclasses
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audl.audit_engine import AuditEngine
from audl.config import AuditConfig, validate_config
from audl.errors import InvalidEventError
from audl.models import (
    EMPTY_HASH,
    EventRecord,
    ManualClock,
    SystemClock,
    canonical_json,
    content_hash,
    format_event_id,
    parse_event_id,
)

T0 = 1_700_000_000_000_000_000  # fixed epoch, ns


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def cfg(**kw):
    base = AuditConfig()
    chain = dataclasses.replace(
        base.chain,
        max_events=kw.get("chain_max", base.chain.max_events),
        max_verify_depth=kw.get("chain_max_verify", base.chain.max_verify_depth),
    )
    query = dataclasses.replace(
        base.query,
        default_limit=kw.get("query_default", base.query.default_limit),
        max_limit=kw.get("query_max", base.query.max_limit),
    )
    dedup = dataclasses.replace(
        base.dedup,
        enabled=kw.get("dedup_enabled", base.dedup.enabled),
        window_ns=kw.get("dedup_window", base.dedup.window_ns),
    )
    return dataclasses.replace(base, chain=chain, query=query, dedup=dedup)


def engine(**kw):
    return AuditEngine(cfg(**kw), clock=ManualClock(T0))


def ev(source="s", kind="k", actor="a", data=None):
    return {"source": source, "kind": kind, "actor": actor, "data": data or {}}


def make_record(seq, data=None, prev_hash=EMPTY_HASH, source="s", kind="k", actor="a"):
    eid = format_event_id(seq)
    payload = {
        "event_id": eid, "ts_ns": T0, "source": source, "kind": kind,
        "actor": actor, "data": data or {}, "prev_hash": prev_hash,
    }
    return EventRecord(**{**payload, "hash": content_hash(payload)})


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_validate_config_defaults_pass():
    assert validate_config(AuditConfig()) == []


def test_validate_config_rejects_bad_values():
    assert validate_config(cfg(chain_max=0)) != []
    assert validate_config(cfg(query_default=5, query_max=1)) != []
    bad_port = dataclasses.replace(AuditConfig(), server=dataclasses.replace(
        AuditConfig().server, listen_port=70000))
    assert validate_config(bad_port) != []
    assert validate_config(cfg(dedup_window=-1)) != []


def test_config_defaults():
    c = AuditConfig()
    assert c.name == "audit-logger" and c.version == "1.0.0"
    assert c.server.listen_port == 7730
    assert c.query.default_limit == 100 and c.query.max_limit == 5000
    assert c.dedup.enabled and c.dedup.window_ns > 0


# ---------------------------------------------------------------------------
# canonical hashing
# ---------------------------------------------------------------------------

def test_canonical_json_key_order_independent():
    a = canonical_json({"b": 1, "a": [1, 2, {"z": 0, "y": 0}]})
    b = canonical_json({"a": [1, 2, {"y": 0, "z": 0}], "b": 1})
    assert a == b == b'{"a":[1,2,{"y":0,"z":0}],"b":1}'


def test_canonical_json_escapes_non_ascii():
    assert canonical_json({"s": "é"}) == b'{"s":"\\u00e9"}'


def test_content_hash_matches_raw_sha256_of_canonical_bytes():
    value = {"kind": "k", "n": 7}
    expected = hashlib.sha256(canonical_json(value)).hexdigest()
    assert content_hash(value) == expected
    assert len(content_hash(value)) == 64


def test_content_hash_differs_on_any_field():
    base = make_record(1, data={"x": 1})
    assert make_record(1, data={"x": 2}).hash != base.hash
    assert make_record(2, data={"x": 1}).hash != base.hash
    assert make_record(1, data={"x": 1}, source="t").hash != base.hash
    assert make_record(1, data={"x": 1}, prev_hash="f" * 64).hash != base.hash


# ---------------------------------------------------------------------------
# event ids
# ---------------------------------------------------------------------------

def test_format_event_id_padding():
    assert format_event_id(0) == "EVT-" + "0" * 12
    assert format_event_id(255) == "EVT-0000000000ff"
    assert format_event_id(0xABCDEF123456) == "EVT-abcdef123456"


def test_parse_event_id_roundtrip():
    for seq in (0, 1, 255, 0xABCDEF123456):
        assert parse_event_id(format_event_id(seq)) == seq


def test_parse_event_id_rejects_malformed():
    for raw in ("", "EVT-", "evt-0000000000ff", "EVT-0000000000fF",
                "EVT-0000000000ff0", "EVT-000000000ff", "EVT-0000000000fg",
                "EVT-00000000001A"):
        assert parse_event_id(raw) is None, raw


# ---------------------------------------------------------------------------
# clocks
# ---------------------------------------------------------------------------

def test_system_clock_reasonable():
    c = SystemClock()
    t1, t2 = c.now_ns(), c.now_ns()
    assert 0 < t1 <= t2


def test_manual_clock_deterministic():
    c = ManualClock(T0)
    assert c.now_ns() == T0
    c.advance(500)
    assert c.now_ns() == T0 + 500
    c.set(7)
    assert c.now_ns() == 7


# ---------------------------------------------------------------------------
# append + chain integrity
# ---------------------------------------------------------------------------

def test_first_record_links_to_empty_hash():
    e = engine()
    out = e.append(ev(data={"i": 1}))
    rec = out.record
    assert out.status == "appended"
    assert rec.prev_hash == EMPTY_HASH
    assert rec.recompute_hash() == rec.hash
    assert rec.ts_ns == T0


def test_chain_links_and_seq_increments():
    e = engine()
    a = e.append(ev(data={"i": 1})).record
    b = e.append(ev(data={"i": 2})).record
    c = e.append(ev(source="t", data={"i": 3})).record
    assert (a.event_id, b.event_id, c.event_id) == (
        "EVT-" + "0" * 12, "EVT-000000000001", "EVT-000000000002")
    assert b.prev_hash == a.hash and c.prev_hash == b.hash
    for r in (a, b, c):
        assert r.recompute_hash() == r.hash
    rep = e.verify_chain()
    assert rep.intact and rep.chain_length == 3 and rep.first_bad_index is None
    assert rep.head_id == a.event_id and rep.tail_hash == c.hash


def test_append_stamps_clock_time_not_request_time():
    e = engine()
    e.append(ev(data={"i": 1}))
    clock = e._clock
    clock.advance(1234)
    out = e.append(ev(data={"i": 2}))  # distinct data → not a dedup fold
    assert out.status == "appended"
    assert out.record.ts_ns == T0 + 1234


def test_reject_ts_ns_in_body():
    e = engine()
    try:
        e.append({"source": "s", "kind": "k", "actor": "a", "ts_ns": T0})
        raise AssertionError("expected InvalidEventError")
    except InvalidEventError as exc:
        assert exc.code == "AUD-201" and exc.context["field"] == "ts_ns"
    assert e.stats()["rejected"] == 1


def test_reject_missing_or_bad_fields():
    e = engine()
    cases = [
        None, [], "x", 42,                      # non-object bodies
        {},                                      # missing everything
        {"source": "s", "kind": "k"},            # missing actor
        {"source": "s", "kind": "k", "actor": "a", "data": [1]},  # data not object
        {"source": "s", "kind": "k", "actor": "a", "data": {"x": float("nan")}},
        {"source": "", "kind": "k", "actor": "a"},
        {"source": "s", "kind": "k", "actor": "a", "data": {"s": "y" * 9000}},
    ]
    for body in cases:
        try:
            e.append(body)
            raise AssertionError(f"expected reject for {body!r}")
        except InvalidEventError as exc:
            assert exc.code == "AUD-201" and exc.http_status == 400
    assert e.stats()["rejected"] == len(cases)
    assert e.stats()["appended"] == 0


def test_data_validation_rejects_deep_nesting_and_bad_types():
    e = engine()
    deep = {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": {"l9": 1}}}}}}}}}
    try:
        e.append({"source": "s", "kind": "k", "actor": "a", "data": deep})
        raise AssertionError("expected InvalidEventError for deep nesting")
    except InvalidEventError as exc:
        assert exc.code == "AUD-201" and "nesting" in exc.message
    ok = e.append({"source": "s", "kind": "k", "actor": "a",
                   "data": {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": 1}}}}}}}}})
    assert ok.status == "appended"  # exactly 8 levels is allowed


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------

def test_duplicate_payload_returns_original_record():
    e = engine()
    first = e.append(ev(data={"i": 1})).record
    out = e.append(ev(data={"i": 1}))
    assert out.status == "duplicate"
    assert out.record.event_id == first.event_id
    assert out.reason and first.event_id in out.reason
    assert e.stats()["duplicates"] == 1
    assert e.stats()["appended"] == 1
    assert e.verify_chain().chain_length == 1


def test_distinct_data_does_not_fold():
    e = engine()
    a = e.append(ev(data={"i": 1})).record
    b = e.append(ev(data={"i": 2}))
    assert b.status == "appended"  # different data → different dedup key
    assert b.record.event_id != a.event_id
    assert e.verify_chain().chain_length == 2


def test_dedup_is_per_source_and_kind():
    e = engine()
    e.append(ev(source="s1", kind="k", data={"i": 1}))
    out = e.append(ev(source="s2", kind="k", data={"i": 1}))
    assert out.status == "appended"  # different source → different key
    out2 = e.append(ev(source="s1", kind="k2", data={"i": 1}))
    assert out2.status == "appended"  # different kind → different key


def test_dedup_window_expiry_allows_reappend():
    e = engine(dedup_window=1_000)
    first = e.append(ev(data={"i": 1})).record
    assert e.append(ev(data={"i": 1})).status == "duplicate"
    e._clock.advance(1_001)  # past the window
    second = e.append(ev(data={"i": 1}))
    assert second.status == "appended"
    assert second.record.event_id != first.event_id
    assert second.record.ts_ns == T0 + 1_001
    assert e.verify_chain().intact


def test_dedup_disabled_appends_everything():
    e = engine(dedup_enabled=False)
    out1 = e.append(ev(data={"i": 1}))
    out2 = e.append(ev(data={"i": 1}))
    out3 = e.append(ev(data={"i": 1}))
    assert out1.status == out2.status == out3.status == "appended"
    assert len({out1.record.event_id, out2.record.event_id, out3.record.event_id}) == 3
    assert e.verify_chain().chain_length == 3
    assert e.stats()["duplicates"] == 0


def test_dedup_max_keys_bounded():
    e = engine()
    e._cfg = dataclasses.replace(e._cfg, dedup=dataclasses.replace(
        e._cfg.dedup, max_keys=3))
    for i in range(5):
        e.append(ev(data={"i": i}))
    assert len(e._dedup) == 3


# ---------------------------------------------------------------------------
# reads: list / get / export
# ---------------------------------------------------------------------------

def test_list_newest_first_with_filters():
    e = engine()
    a = e.append(ev(source="s1", kind="k1", data={"i": 1})).record
    b = e.append(ev(source="s2", kind="k2", data={"i": 2})).record
    c = e.append(ev(source="s1", kind="k3", data={"i": 3})).record
    ids = [r.event_id for r in e.list_events(limit=10)]
    assert ids == [c.event_id, b.event_id, a.event_id]
    src = [r.event_id for r in e.list_events(source="s1")]
    assert src == [c.event_id, a.event_id]
    kind = [r.event_id for r in e.list_events(kind="k2")]
    assert kind == [b.event_id]
    limited = e.list_events(limit=2)
    assert [r.event_id for r in limited] == [c.event_id, b.event_id]
    assert e.list_events(limit=0) == []


def test_get_returns_record_or_none():
    e = engine()
    rec = e.append(ev()).record
    got = e.get(rec.event_id)
    assert got is not None and got.event_id == rec.event_id
    assert e.get("EVT-ffffffffffff") is None


def test_export_lines_oldest_first_and_json_parses():
    import json
    e = engine()
    a = e.append(ev(source="s1", data={"i": 1})).record
    b = e.append(ev(source="s2", data={"i": 2})).record
    lines = e.export_lines()
    assert [json.loads(x)["event_id"] for x in lines] == [a.event_id, b.event_id]
    filtered = e.export_lines(source="s2")
    assert [json.loads(x)["event_id"] for x in filtered] == [b.event_id]
    bounded = e.export_lines(limit=1)
    assert len(bounded) == 1
    # each line is exactly one compact JSON object with all 8 fields
    obj = json.loads(lines[0])
    assert set(obj) == {"event_id", "ts_ns", "source", "kind", "actor",
                        "data", "prev_hash", "hash"}
    assert lines[0] == json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# verification + tamper detection
# ---------------------------------------------------------------------------

def test_verify_report_on_empty_chain():
    rep = engine().verify_chain()
    assert rep.intact and rep.chain_length == 0 and rep.head_id is None
    assert rep.first_bad_index is None and not rep.truncated


def test_verify_chain_detects_tampered_content_hash():
    e = engine()
    e.append(ev(data={"i": 1}))
    e.append(ev(data={"i": 2}))
    victim = e.list_events(limit=10)[1]  # oldest of the two (chain index 0)
    tampered = dataclasses.replace(victim, data={"i": 999})
    e._records[victim.event_id] = tampered
    rep = e.verify_chain()
    assert not rep.intact
    assert rep.first_bad_index == 0 and rep.first_bad_id == victim.event_id
    assert "content hash mismatch" in rep.reason
    assert rep.verified_count == 1


def test_verify_chain_detects_broken_prev_link():
    from audl.models import content_hash
    e = engine()
    e.append(ev(data={"i": 1}))
    b = e.append(ev(data={"i": 2})).record
    # break the link AND re-hash the record so only the prev-hash link fails
    bad_prev = "f" * 64
    cut = dataclasses.replace(b, prev_hash=bad_prev)
    e._records[b.event_id] = dataclasses.replace(cut, hash=content_hash(cut.hash_input()))
    rep = e.verify_chain()
    assert not rep.intact
    assert rep.first_bad_index == 1 and rep.first_bad_id == b.event_id
    assert "prev_hash link broken" in rep.reason


def test_verify_chain_detects_swapped_records():
    e = engine()
    a = e.append(ev(source="a", data={"i": 1})).record
    b = e.append(ev(source="b", data={"i": 2})).record
    e._records[b.event_id], e._records[a.event_id] = a, b  # swap order
    rep = e.verify_chain()
    assert not rep.intact
    assert rep.first_bad_index is not None


def test_verify_single_record_ok_and_bad():
    e = engine()
    rec = e.append(ev()).record
    ok, reason = e.verify(rec)
    assert ok and reason == ""
    bad, why = e.verify(dataclasses.replace(rec, data={"x": 1}))
    assert not bad and "content hash mismatch" in why
    orphan = make_record(99, data={"i": 1})
    ok2, why2 = e.verify(orphan)
    assert not ok2 and "not in the chain" in why2


def test_verify_chain_truncation_report():
    e = AuditEngine(cfg(chain_max=100, chain_max_verify=4), clock=ManualClock(T0))
    for i in range(6):
        e.append(ev(data={"i": i}))
    rep = e.verify_chain()
    assert rep.truncated and not rep.intact
    assert rep.verified_count == 4 and rep.chain_length == 6
    assert rep.first_bad_index is None  # truncated, not broken


# ---------------------------------------------------------------------------
# eviction
# ---------------------------------------------------------------------------

def test_eviction_severs_chain_and_stays_verifiable():
    e = engine(chain_max=3)
    ids = [e.append(ev(data={"i": i})).record.event_id for i in range(5)]
    rep = e.verify_chain()
    assert rep.intact and rep.chain_length == 3
    assert [r.event_id for r in e.list_events(limit=10)] == list(reversed(ids[2:]))
    head = e.oldest_id()
    assert head == ids[2]
    assert e.get(head).prev_hash == EMPTY_HASH  # re-severed head
    assert e.stats()["evicted"] == 2
    assert e.stats()["head_id"] == head
    # the full surviving suffix still verifies end-to-end
    first = e.get(ids[2])
    last = e.get(ids[4])
    assert e.verify_chain().tail_hash == last.hash
    assert first is not None and first.recompute_hash() == first.hash


def test_evicted_id_reports_evicted_flag():
    e = engine(chain_max=2)
    first = e.append(ev(data={"i": 1})).record
    e.append(ev(data={"i": 2}))
    e.append(ev(data={"i": 3}))
    assert e.get(first.event_id) is None
    assert e.oldest_id() is not None
    from audl.models import parse_event_id as pei
    assert pei(e.oldest_id()) > pei(first.event_id)


def test_append_after_eviction_links_to_rehashed_tail():
    e = engine(chain_max=2)
    a = e.append(ev(data={"i": 1})).record
    e.append(ev(data={"i": 2}))  # evicts a, re-chains #2
    new = e.append(ev(data={"i": 3}))  # evicts #2, re-chains, links to #2's new hash
    assert new.status == "appended"
    assert e.get(a.event_id) is None
    pred = e.list_events(limit=10)[1]  # older of the two survivors = re-chained #2
    assert new.record.prev_hash == pred.hash
    assert e.verify_chain().intact and e.verify_chain().chain_length == 2
    # the returned record is the stored one (never invalidated by re-chaining)
    stored = e.get(new.record.event_id)
    assert stored.hash == new.record.hash and stored.prev_hash == new.record.prev_hash


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_stats_counters_and_breakdowns():
    e = engine()
    e.append(ev(source="s1", kind="k1", data={"i": 1}))
    e.append(ev(source="s2", kind="k2", data={"i": 2}))
    e.append(ev(source="s1", kind="k2", data={"i": 3}))
    e.append(ev(source="s1", kind="k1", data={"i": 1}))  # duplicate
    try:
        e.append(None)
    except InvalidEventError:
        pass
    s = e.stats()
    assert s["chain_length"] == 3 and s["appended"] == 3
    assert s["duplicates"] == 1 and s["rejected"] == 1 and s["evicted"] == 0
    assert s["by_source"] == {"s1": 2, "s2": 1}
    assert s["by_kind"] == {"k1": 1, "k2": 2}
    assert s["last_verified"]["intact"] is True
    assert s["head_id"] == format_event_id(0)


def test_stats_empty_chain():
    s = engine().stats()
    assert s["chain_length"] == 0 and s["appended"] == 0
    assert s["head_id"] is None and s["by_source"] == {} and s["by_kind"] == {}
    assert s["tail_hash"] == EMPTY_HASH
    assert s["last_verified"]["intact"] is True
