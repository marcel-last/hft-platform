"""config_service — unit tests for the configuration store core.

Covers: canonical JSON / content hash, deep merge, config CRUD + versioning +
bounded history, environment overrides (set/clear/list + read-time merge),
feature flags (+ bounded history), change log + long-poll (immediate wake and
timeout), error taxonomy + envelope, and a full HTTP end-to-end pass.

Runnable with:  cd services/config-service && python -m pytest tests/ -v
No network calls in unit tests except the single self-contained HTTP E2E which
boots the service on an ephemeral port.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from cfgs.config import (CONFIG, FlagConfig, PollConfig, ServiceConfig,
                        StoreConfig, validate_config)
from cfgs.config_store import ConfigStore
from cfgs.errors import (
    CFGConfigError,
    CFGError,
    InvalidFlagError,
    InvalidPayloadError,
    StoreFullError,
    UnknownServiceError,
    error_envelope,
)
from cfgs.models import ChangeEvent, ConfigBlob, content_hash, deep_merge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(**overrides) -> ConfigStore:
    """Build a store with small bounds so retention limits are easy to hit."""
    cfg = ServiceConfig(
        store=StoreConfig(
            max_revisions_per_service=overrides.get("max_revisions", 3),
            max_change_events=overrides.get("max_changes", 50),
            max_services=overrides.get("max_services", 100),
        ),
        flags=FlagConfig(
            max_flags=overrides.get("max_flags", 100),
            max_flag_history=overrides.get("max_flag_history", 3),
        ),
        poll=PollConfig(
            default_timeout_ms=200,
            max_timeout_ms=500,
            poll_granularity_ms=10,
        ),
    )
    return ConfigStore(cfg)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_canonical_json_is_order_independent():
    from cfgs.models import canonical_json
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert canonical_json(a) == canonical_json(b)


def test_content_hash_stable_and_distinct():
    assert content_hash({"x": 1}) == content_hash({"x": 1})
    assert content_hash({"x": 1}) != content_hash({"x": 2})
    # key order must not matter
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_deep_merge_nested():
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "lst": [1, 2]}
    over = {"nested": {"y": 99, "z": 3}, "lst": [9], "c": 4}
    out = deep_merge(base, over)
    assert out == {"a": 1, "nested": {"x": 1, "y": 99, "z": 3}, "lst": [9], "c": 4}
    # base must not be mutated
    assert base["nested"] == {"x": 1, "y": 2}


def test_deep_merge_non_dict_override():
    assert deep_merge({"a": 1}, None) == {"a": 1}
    assert deep_merge(None, {"a": 1}) == {"a": 1}


# ---------------------------------------------------------------------------
# Config CRUD + versioning
# ---------------------------------------------------------------------------

def test_upsert_creates_then_updates():
    s = make_store()
    blob, created = s.upsert("strategy-engine", {"cooldown_ms": 50}, updated_by="ops")
    assert created is True
    assert blob.revision == 1
    assert blob.hash == content_hash({"cooldown_ms": 50})

    blob2, created2 = s.upsert("strategy-engine", {"cooldown_ms": 75}, updated_by="ops")
    assert created2 is False
    assert blob2.revision == 2
    assert blob2.hash != blob.hash


def test_get_merges_override_and_reports():
    s = make_store()
    s.upsert("obb", {"stale_ms": 100, "depth": 5})
    data = s.get("obb")
    assert data["payload"] == {"stale_ms": 100, "depth": 5}
    assert data["override_applied"] is False
    assert data["revision"] == 1

    s.set_override("obb", "production", {"stale_ms": 200})
    data2 = s.get("obb")
    assert data2["payload"] == {"stale_ms": 200, "depth": 5}
    assert data2["override_applied"] is True
    # base hash unchanged, merged hash differs
    assert data2["base_hash"] == data["hash"]
    assert data2["hash"] != data["hash"]


def test_get_unknown_service_raises():
    s = make_store()
    with pytest.raises(UnknownServiceError):
        s.get("nope")


def test_upsert_non_dict_payload_raises():
    s = make_store()
    with pytest.raises(InvalidPayloadError):
        s.upsert("obb", [1, 2, 3])


def test_revision_history_is_bounded():
    s = make_store(max_revisions=3)
    for i in range(6):
        s.upsert("svc", {"i": i})
    v = s.versions("svc")
    assert v["current_revision"] == 6
    # only the last 3 prior revisions retained (revs 3,4,5)
    hist_revs = [h["revision"] for h in v["history"]]
    assert hist_revs == [3, 4, 5]


def test_store_full_enforced():
    s = make_store(max_services=2)
    s.upsert("a", {"x": 1})
    s.upsert("b", {"x": 1})
    with pytest.raises(StoreFullError):
        s.upsert("c", {"x": 1})
    # updating an existing service is still allowed at the cap
    s.upsert("a", {"x": 2})


# ---------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------

def test_override_set_clear_list():
    s = make_store()
    s.upsert("svc", {"k": 1})
    s.set_override("svc", "staging", {"k": 2, "extra": True})
    ov = s.list_overrides("svc")
    assert ov["overrides"] == {"staging": {"k": 2, "extra": True}}

    res = s.clear_override("svc", "staging")
    assert res["found"] is True and res["cleared"] == ["staging"]
    assert s.list_overrides("svc")["overrides"] == {}

    # clearing again reports not found
    res2 = s.clear_override("svc", "staging")
    assert res2["found"] is False


def test_clear_all_envs():
    s = make_store()
    s.upsert("svc", {"k": 1})
    s.set_override("svc", "dev", {"a": 1})
    s.set_override("svc", "staging", {"b": 2})
    res = s.clear_override("svc")
    assert sorted(res["cleared"]) == ["dev", "staging"]
    assert s.list_overrides("svc")["overrides"] == {}


def test_override_per_env_isolation():
    s = make_store()
    s.upsert("svc", {"k": 1})
    s.set_override("svc", "dev", {"k": 100})
    # production read is unaffected by the dev override
    prod = s.get("svc", env="production")
    assert prod["payload"]["k"] == 1
    dev = s.get("svc", env="dev")
    assert dev["payload"]["k"] == 100


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

def test_flag_set_get_list():
    s = make_store()
    s.set_flag("fast_path", True, description="enable fast path")
    flag = s.get_flag("fast_path")
    assert flag.value is True
    assert flag.description == "enable fast path"
    assert [f.name for f in s.list_flags()] == ["fast_path"]

    s.set_flag("fast_path", False)
    assert s.get_flag("fast_path").value is False


def test_flag_history_is_bounded():
    s = make_store(max_flag_history=3)
    for i in range(5):
        s.set_flag("f", i)
    flag = s.get_flag("f")
    assert flag.value == 4
    assert len(flag.history) == 3
    # history retains the most recent values
    assert [h["value"] for h in flag.history] == [2, 3, 4]


def test_get_unknown_flag_raises():
    s = make_store()
    with pytest.raises(InvalidFlagError):
        s.get_flag("missing")


def test_flag_limit_enforced():
    s = make_store(max_flags=2)
    s.set_flag("a", 1)
    s.set_flag("b", 2)
    with pytest.raises(InvalidFlagError):
        s.set_flag("c", 3)
    # updating an existing flag is still allowed at the cap
    s.set_flag("a", 9)


# ---------------------------------------------------------------------------
# Change log + long-poll
# ---------------------------------------------------------------------------

def test_change_log_sequenced_and_filtered():
    s = make_store()
    s.upsert("a", {"x": 1})      # seq 1 (config_update, service a)
    s.set_flag("f", True)        # seq 2 (flag_change, service "f")
    s.upsert("b", {"y": 2})      # seq 3 (config_update, service b)

    all_events = s.changes(since=0)
    assert [e["seq"] for e in all_events] == [1, 2, 3]

    after_1 = s.changes(since=1)
    assert [e["seq"] for e in after_1] == [2, 3]


def test_wait_for_change_returns_immediately_when_already_changed():
    s = make_store()
    s.upsert("a", {"x": 1})
    res = s.wait_for_change(since=0)
    assert res["changed"] is True
    assert res["latest_seq"] == 1
    assert len(res["events"]) >= 1


def test_wait_for_change_wakes_on_new_event():
    s = make_store()
    start = time.monotonic()

    def mutate_later():
        time.sleep(0.05)
        s.upsert("a", {"x": 1})

    t = threading.Thread(target=mutate_later)
    t.start()
    res = s.wait_for_change(since=0, timeout_ms=500)
    t.join()
    elapsed = time.monotonic() - start
    assert res["changed"] is True
    # woke well before the 500ms timeout thanks to the new event
    assert elapsed < 0.4


def test_wait_for_change_times_out_when_quiet():
    s = make_store()
    start = time.monotonic()
    res = s.wait_for_change(since=0, timeout_ms=80)
    elapsed = time.monotonic() - start
    assert res["changed"] is False
    assert res["latest_seq"] == 0
    assert res["events"] == []
    assert elapsed >= 0.06


def test_wait_for_change_scoped_to_service():
    s = make_store()
    s.upsert("a", {"x": 1})   # seq 1 for service a (already known to the watcher)

    def mutate_other():
        time.sleep(0.05)
        s.upsert("b", {"y": 2})   # a different service

    t = threading.Thread(target=mutate_other)
    t.start()
    # Watch for NEW changes to service "a" only (since=1). Service "b"'s change
    # must NOT wake this scoped watch, so it times out with changed=False.
    res = s.wait_for_change(since=1, timeout_ms=120, service="a")
    t.join()
    assert res["changed"] is False  # scoped watch ignores b's event

    # A subsequent change to "a" IS delivered by a fresh scoped watch.
    s.upsert("a", {"x": 3})
    res2 = s.wait_for_change(since=1, timeout_ms=50, service="a")
    assert res2["changed"] is True
    assert all(e["service"] == "a" for e in res2["events"])


def test_change_log_is_bounded():
    s = make_store(max_changes=5)
    for i in range(10):
        s.upsert("svc", {"i": i})
    events = s.changes(since=0)
    assert len(events) <= 5
    # keeps the most recent
    assert [e["seq"] for e in events] == list(range(6, 11))


# ---------------------------------------------------------------------------
# Stats + readiness
# ---------------------------------------------------------------------------

def test_stats_view_counts():
    s = make_store()
    s.upsert("a", {"x": 1})
    s.set_override("a", "dev", {"x": 2})
    s.set_flag("f", True)
    st = s.stats_view()
    assert st["services_tracked"] == 1
    assert st["overrides_active"] == 1
    assert st["flags"] == 1
    assert st["upserts_total"] == 1
    assert st["overrides_set_total"] == 1
    assert st["flag_changes_total"] == 1
    assert st["latest_seq"] >= 3


def test_readiness_reasons():
    s = make_store()
    assert s.readiness_reasons() == ["no service configurations stored yet"]
    s.upsert("a", {"x": 1})
    assert s.readiness_reasons() == []


# ---------------------------------------------------------------------------
# Error taxonomy + envelope
# ---------------------------------------------------------------------------

def test_error_envelope_shape():
    env = error_envelope(UnknownServiceError("ghost"))
    e = env["error"]
    assert set(e.keys()) == {"code", "message", "service", "retryable", "context"}
    assert e["code"] == "CFG-203"
    assert e["service"] == "config-service"
    assert e["retryable"] is False
    assert e["context"]["service"] == "ghost"


def test_error_envelope_unknown_exception():
    env = error_envelope(ValueError("boom"))
    assert env["error"]["code"] == "CFG-999"
    assert env["error"]["service"] == "config-service"


def test_http_status_mapping():
    assert UnknownServiceError("x").http_status == 404
    assert InvalidPayloadError("x").http_status == 400
    assert StoreFullError(1).http_status == 409
    assert CFGConfigError("bad").http_status == 503


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_validate_config_default_is_valid():
    assert validate_config(CONFIG) == []


def test_validate_config_catches_bad_values():
    bad = ServiceConfig(listen_port=0, poll=type(CONFIG.poll)(default_timeout_ms=100, max_timeout_ms=50))
    errors = validate_config(bad)
    assert any("listen_port" in e for e in errors)
    assert any("max_timeout_ms" in e for e in errors)


# ---------------------------------------------------------------------------
# HTTP end-to-end (boots the real service on an ephemeral port)
# ---------------------------------------------------------------------------

def _http(method: str, url: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_end_to_end():
    import socket
    from cfgs.config_store import ConfigStore
    from cfgs.controller import ConfigController
    from cfgs.main import serve_http
    from cfgs.router import build_router

    # find a free port
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    store = make_store()
    controller = ConfigController()
    controller.store = store
    router = build_router(controller)
    httpd = serve_http(router, controller, "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"

    try:
        # healthz
        st, body = _http("GET", f"{base}/healthz")
        assert st == 200 and body["status"] == "ok"
        assert body["service"] == "config-service"

        # readyz (empty store -> not_ready)
        st, body = _http("GET", f"{base}/readyz")
        assert st == 503 and body["status"] == "not_ready"
        assert body["reasons"]

        # PUT a config
        st, body = _http("PUT", f"{base}/config/strategy-engine",
                         {"payload": {"cooldown_ms": 42}, "updated_by": "tester"})
        assert st == 200 and body["created"] is True
        assert body["config"]["revision"] == 1

        # readyz now ready
        st, body = _http("GET", f"{base}/readyz")
        assert st == 200 and body["status"] == "ready"

        # GET effective config
        st, body = _http("GET", f"{base}/config/strategy-engine")
        assert st == 200
        assert body["config"]["payload"] == {"cooldown_ms": 42}
        assert body["config"]["override_applied"] is False

        # PUT again -> revision 2
        st, body = _http("PUT", f"{base}/config/strategy-engine",
                         {"payload": {"cooldown_ms": 99}})
        assert st == 200 and body["created"] is False
        assert body["config"]["revision"] == 2

        # GET /configs
        st, body = _http("GET", f"{base}/configs")
        assert st == 200 and body["count"] == 1
        assert body["services"][0]["service"] == "strategy-engine"

        # set an override via POST
        st, body = _http("POST", f"{base}/overrides",
                         {"service": "strategy-engine", "env": "staging",
                          "override": {"cooldown_ms": 7}})
        assert st == 200 and body["found"] is not False

        # GET with env=staging reflects the override
        st, body = _http("GET", f"{base}/config/strategy-engine?env=staging")
        assert st == 200
        assert body["config"]["payload"]["cooldown_ms"] == 7
        assert body["config"]["override_applied"] is True

        # GET without env uses blob's default (production) -> unmerged
        st, body = _http("GET", f"{base}/config/strategy-engine")
        assert body["config"]["payload"]["cooldown_ms"] == 99

        # clear override
        st, body = _http("DELETE", f"{base}/overrides",
                         {"service": "strategy-engine", "env": "staging"})
        assert st == 200 and body["found"] is True

        # versions (global)
        st, body = _http("GET", f"{base}/versions")
        assert st == 200 and body["count"] == 1

        # versions for a service (history present)
        st, body = _http("GET", f"{base}/versions?service=strategy-engine")
        assert st == 200 and body["current_revision"] == 2
        assert len(body["history"]) == 1

        # changes log
        st, body = _http("GET", f"{base}/changes")
        assert st == 200 and body["count"] >= 3
        kinds = {e["kind"] for e in body["events"]}
        assert "config_update" in kinds and "override_set" in kinds

        # flags
        st, body = _http("PUT", f"{base}/flags/fast_path", {"value": True})
        assert st == 200 and body["flag"]["value"] is True
        st, body = _http("GET", f"{base}/flags")
        assert st == 200 and body["count"] == 1

        # long-poll returns immediately when already changed
        st, body = _http("GET", f"{base}/changes/watch?since=0&timeout_ms=50")
        assert st == 200 and body["changed"] is True

        # stats
        st, body = _http("GET", f"{base}/stats")
        assert st == 200 and body["store"]["services_tracked"] == 1

        # unknown service -> 404 CFG-203
        st, body = _http("GET", f"{base}/config/ghost")
        assert st == 404 and body["error"]["code"] == "CFG-203"

        # no route -> 404 CFG-404
        st, body = _http("GET", f"{base}/does-not-exist")
        assert st == 404 and body["error"]["code"] == "CFG-404"
    finally:
        httpd.shutdown()
