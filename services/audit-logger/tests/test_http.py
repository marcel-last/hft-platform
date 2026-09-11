"""S13 audit-logger HTTP end-to-end tests.

Every test runs a real ``ThreadingHTTPServer`` on ``127.0.0.1:0`` (ephemeral
port, no fixed port, no external network — CONVENTIONS §10).  Envelopes are
checked field-by-field against CONVENTIONS §1.2.

Run: ``cd services/audit-logger && python -m pytest tests/ -q``
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audl.audit_engine import AuditEngine
from audl.config import AuditConfig
from audl.controller import AuditController
from audl.main import _HTTPHandler
from audl.models import EMPTY_HASH, ManualClock
from audl.router import build_router

T0 = 1_700_000_000_000_000_000


def start_server(cfg=None, clock=None):
    cfg = cfg or AuditConfig()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HTTPHandler)
    port = httpd.server_address[1]
    engine = AuditEngine(cfg, clock=clock or ManualClock(T0))
    controller = AuditController(cfg=cfg, engine=engine)
    _HTTPHandler.router = build_router(controller)
    _HTTPHandler.controller = controller
    threading.Thread(target=httpd.serve_forever, name="audl-test-http", daemon=True).start()
    return httpd, port, engine


def request(port, method, path, body=None, raw_body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {}
    if raw_body is not None:
        data = raw_body
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else None


def assert_envelope(status, code, obj, expected_status):
    """Assert exact §1.2 envelope: HTTP status + code + service + shape."""
    assert status == expected_status, f"status {status} != {expected_status}"
    assert isinstance(obj, dict) and set(obj) == {"error"}, obj
    err = obj["error"]
    assert set(err) == {"code", "message", "service", "retryable", "context"}, err
    assert err["code"] == code, err
    assert err["service"] == "audit-logger", err
    assert isinstance(err["message"], str) and err["message"], err
    assert isinstance(err["retryable"], bool), err
    assert isinstance(err["context"], dict), err
    return err


def ev(source="execution-gateway", kind="order.submitted", actor="ste", data=None):
    return {"source": source, "kind": kind, "actor": actor, "data": data or {}}


# ---------------------------------------------------------------------------
# health / ready
# ---------------------------------------------------------------------------

def test_healthz_exact():
    httpd, port, _ = start_server()
    try:
        status, body = request(port, "GET", "/healthz")
        assert status == 200
        assert body == {"status": "ok", "service": "audit-logger", "version": "1.0.0"}
    finally:
        httpd.shutdown()


def test_readyz_exact():
    httpd, port, _ = start_server()
    try:
        status, body = request(port, "GET", "/readyz")
        assert status == 200
        assert body == {"status": "ready", "reasons": []}
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# append + read round trip
# ---------------------------------------------------------------------------

def test_append_and_get_roundtrip():
    httpd, port, _ = start_server()
    try:
        status, body = request(port, "POST", "/events", body=ev(data={"order_id": "O1"}))
        assert status == 200
        assert body["status"] == "appended"
        rec = body["event"]
        assert rec["event_id"] == "EVT-" + "0" * 12
        assert rec["prev_hash"] == EMPTY_HASH
        assert rec["source"] == "execution-gateway"
        assert rec["data"] == {"order_id": "O1"}
        assert rec["hash"] and len(rec["hash"]) == 64
        # self-consistency: recomputing the hash from the returned dict matches
        input_dict = {k: rec[k] for k in
                      ("event_id", "ts_ns", "source", "kind", "actor", "data", "prev_hash")}
        from audl.models import content_hash
        assert content_hash(input_dict) == rec["hash"]

        status, body = request(port, "GET", f"/events/{rec['event_id']}")
        assert status == 200
        assert body == {"event": rec, "verified": True, "reason": ""}
    finally:
        httpd.shutdown()


def test_list_newest_first_filters_and_limit():
    httpd, port, _ = start_server()
    try:
        ids = []
        for i, (src, kind) in enumerate([("s1", "k1"), ("s2", "k2"), ("s1", "k3")]):
            _, body = request(port, "POST", "/events",
                              body=ev(source=src, kind=kind, data={"i": i}))
            ids.append(body["event"]["event_id"])
        _, body = request(port, "GET", "/events?limit=10")
        assert body["count"] == 3
        assert [e["event_id"] for e in body["events"]] == list(reversed(ids))
        _, body = request(port, "GET", "/events?source=s1")
        assert [e["event_id"] for e in body["events"]] == [ids[2], ids[0]]
        _, body = request(port, "GET", "/events?kind=k2")
        assert [e["event_id"] for e in body["events"]] == [ids[1]]
        _, body = request(port, "GET", "/events?limit=2")
        assert body["count"] == 2 and len(body["events"]) == 2
    finally:
        httpd.shutdown()


def test_export_lines_oldest_first():
    httpd, port, _ = start_server()
    try:
        ids = []
        for i in range(3):
            _, body = request(port, "POST", "/events", body=ev(data={"i": i}))
            ids.append(body["event"]["event_id"])
        status, body = request(port, "GET", "/events/export")
        assert status == 200
        assert body["format"] == "json-lines" and body["order"] == "oldest_first"
        assert body["count"] == 3
        assert [json.loads(line)["event_id"] for line in body["lines"]] == ids
        # filtered + bounded
        _, body = request(port, "GET", "/events/export?limit=1")
        assert body["count"] == 1 and json.loads(body["lines"][0])["event_id"] == ids[0]
        _, body = request(port, "GET", "/events/export?kind=order.submitted&limit=5000")
        assert body["count"] == 3  # cap applied, all match
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# verify chain
# ---------------------------------------------------------------------------

def test_verify_chain_clean_and_tamper_detected():
    httpd, port, engine = start_server()
    try:
        for i in range(3):
            request(port, "POST", "/events", body=ev(data={"i": i}))
        status, body = request(port, "GET", "/events/verify-chain")
        assert status == 200
        assert body["intact"] is True
        assert body["chain_length"] == 3 and body["verified_count"] == 3
        assert body["truncated"] is False and "first_bad_index" not in body

        # tamper the oldest record's data directly inside the engine
        ordered = engine.list_events(limit=10)  # newest-first → index 2 = chain 0
        mid = ordered[2]
        engine._records[mid.event_id] = replace(mid, data={"i": 999})
        status, body = request(port, "GET", "/events/verify-chain")
        assert status == 207
        assert body["intact"] is False
        assert body["first_bad_index"] == 0 and body["first_bad_id"] == mid.event_id
        assert "content hash mismatch" in body["reason"]
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------

def test_error_envelopes_exact():
    httpd, port, _ = start_server()
    try:
        # bad JSON body → AUD-001 400
        status, body = request(port, "POST", "/events", raw_body=b"{not json")
        err = assert_envelope(status, "AUD-001", body, 400)
        assert err["retryable"] is False

        # missing fields → AUD-201 400 with field context
        status, body = request(port, "POST", "/events", body={"source": "s"})
        err = assert_envelope(status, "AUD-201", body, 400)
        assert err["context"]["field"] == "kind"

        # ts_ns rejected → AUD-201
        status, body = request(port, "POST", "/events", body={**ev(), "ts_ns": T0})
        err = assert_envelope(status, "AUD-201", body, 400)
        assert err["context"]["field"] == "ts_ns"

        # malformed id → AUD-202 400
        status, body = request(port, "GET", "/events/not-an-id")
        err = assert_envelope(status, "AUD-202", body, 400)
        assert err["context"]["raw"] == "not-an-id"

        # unknown id → AUD-204 404
        status, body = request(port, "GET", "/events/EVT-ffffffffffff")
        err = assert_envelope(status, "AUD-204", body, 404)
        assert err["context"]["event_id"] == "EVT-ffffffffffff"

        # bad limit → AUD-203 400
        status, body = request(port, "GET", "/events?limit=abc")
        err = assert_envelope(status, "AUD-203", body, 400)
        assert err["context"]["raw"] == "abc"

        # no route → AUD-404 404 with method+path context
        status, body = request(port, "GET", "/nope")
        err = assert_envelope(status, "AUD-404", body, 404)
        assert err["context"] == {"method": "GET", "path": "/nope"}

        # wrong method on a known path → AUD-404 (router-level no-route)
        status, body = request(port, "DELETE", "/events")
        err = assert_envelope(status, "AUD-404", body, 404)
        assert err["context"] == {"method": "DELETE", "path": "/events"}
    finally:
        httpd.shutdown()


def test_duplicate_append_returns_original():
    httpd, port, _ = start_server()
    try:
        _, first = request(port, "POST", "/events", body=ev(data={"i": 1}))
        assert first["status"] == "appended"
        status, second = request(port, "POST", "/events", body=ev(data={"i": 1}))
        assert status == 200
        assert second["status"] == "duplicate"
        assert second["event"]["event_id"] == first["event"]["event_id"]
        assert second["event"]["hash"] == first["event"]["hash"]
        assert "reason" in second
        _, stats = request(port, "GET", "/stats")
        assert stats["duplicates"] == 1 and stats["appended"] == 1
        assert stats["chain_length"] == 1
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_stats_shape_and_counters():
    httpd, port, _ = start_server()
    try:
        request(port, "POST", "/events", body=ev(source="s1", kind="k1", data={"i": 1}))
        request(port, "POST", "/events", body=ev(source="s2", kind="k2", data={"i": 2}))
        request(port, "POST", "/events", body=ev(source="s1", kind="k2", data={"i": 3}))
        status, body = request(port, "GET", "/stats")
        assert status == 200
        assert body["chain_length"] == 3 and body["appended"] == 3
        assert body["duplicates"] == 0 and body["rejected"] == 0 and body["evicted"] == 0
        assert body["by_source"] == {"s1": 2, "s2": 1}
        assert body["by_kind"] == {"k1": 1, "k2": 2}
        assert body["head_id"] == "EVT-" + "0" * 12
        assert body["last_verified"]["intact"] is True
    finally:
        httpd.shutdown()


def test_full_append_verify_export_roundtrip():
    """The canonical S13 round trip: append → verify-chain → export → rehash."""
    httpd, port, _ = start_server()
    try:
        stored = []
        for i in range(4):
            _, body = request(port, "POST", "/events",
                              body=ev(source=f"s{i % 2}", kind=f"k{i % 3}", data={"i": i}))
            stored.append(body["event"])
        _, v = request(port, "GET", "/events/verify-chain")
        assert v["intact"] is True and v["chain_length"] == 4
        _, x = request(port, "GET", "/events/export?limit=100")
        lines = x["lines"]
        assert len(lines) == 4
        for line, rec in zip(lines, stored):
            obj = json.loads(line)
            assert obj == rec  # wire representation identical to stored record
            from audl.models import content_hash
            input_dict = {k: obj[k] for k in
                          ("event_id", "ts_ns", "source", "kind", "actor", "data", "prev_hash")}
            assert content_hash(input_dict) == obj["hash"]
        # chain links hold across the export
        assert lines[0] and json.loads(lines[1])["prev_hash"] == json.loads(lines[0])["hash"]
    finally:
        httpd.shutdown()
