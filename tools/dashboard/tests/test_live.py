"""M2 tests: aggregator loop, SSE frame format, keepalive cadence.

All time is injected (``clock`` callable), so the keepalive cadence is
deterministic — no sleeping anywhere (CONVENTIONS §10).  Pollers are
faked via :class:`FakeTransport` (no sockets); the ``serve_stream`` test
uses an in-memory fake handler.
"""

import json
import threading
import time

from dash.live import (
    LiveConfig,
    LiveSession,
    StreamRequest,
    build_envelope,
    build_sources,
    format_frame,
    serve_stream,
)
from dash.live_sources import Source, SourceTransportError


# t0.  Must be small enough that tick offsets stay well inside int64 range
# (keepalive math is int64 nanoseconds, CONVENTIONS §2).
BASE_NS = 1_000_000_000_000_000
CFG = LiveConfig(poll_interval_s=0.01, read_timeout_s=1.5,
                 keepalive_interval_s=5.0, max_age_s=3600.0,
                 queue_maxsize=1000)


def _manual_clock(start_ns=BASE_NS):
    box = [start_ns]

    def clock() -> int:
        return box[0]

    clock.tick = lambda s: box.__setitem__(0, box[0] + s)  # type: ignore[attr-defined]
    return clock


class _FakeSource(Source):
    """Scripted source: canned outcomes per poll, no transport at all."""

    def __init__(self, name, outcomes):
        super().__init__(name, name, None, "http://fake", 1.5, "")
        self._outcomes = list(outcomes)
        self.polls = 0

    def poll_once(self):
        i = min(self.polls, len(self._outcomes) - 1)
        self.polls += 1
        outcome = self._outcomes[i]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _session(outcomes_by_name, clock=None, **cfg_over):
    cfg = LiveConfig(**cfg_over) if cfg_over else CFG
    sources = [_FakeSource(name, outcomes) for name, outcomes in
               outcomes_by_name.items()]
    return LiveSession(StreamRequest([s.name for s in sources], [], 3600.0),
                       sources, cfg, clock=clock or _manual_clock())


# -- SSE frame format (PLAN §3.2) ------------------------------------------

def test_frame_format_is_data_json_blank_line():
    env = build_envelope("mdg", "quote", 123, {"px": 1.5})
    frame = format_frame(env)
    assert frame == (b'data: {"src":"mdg","kind":"quote","ts":123,'
                     b'"data":{"px":1.5}}\n\n')


def test_envelope_has_exactly_the_documented_keys():
    env = build_envelope("altsvc", "alert", 7)
    assert set(env) == {"src", "kind", "ts", "data"}
    assert env["data"] == {}


# -- keepalive cadence with an injected clock -------------------------------

def test_keepalive_every_5s_never_earlier():
    clock = _manual_clock()
    session = _session({"mdg": []}, clock=clock)  # no payloads, ever
    deadline = clock() + 3600 * 1_000_000_000
    frames = session.drain(clock(), deadline)
    assert frames == []                       # t0: nothing due yet
    frames = session.drain(clock(), deadline)
    assert frames == []                       # still t0
    clock.tick(5 * 1_000_000_000 - 1)
    assert session.drain(clock(), deadline) == []  # just under 5 s: none
    clock.tick(1)                              # exactly 5 s
    frames = session.drain(clock(), deadline)
    assert len(frames) == 1
    env = json.loads(frames[0][5:-2])
    assert env["kind"] == "keepalive" and env["src"] == "dashboard"
    assert env["ts"] == clock()
    # the same tick must not emit a second keepalive
    assert session.drain(clock(), deadline) == []
    clock.tick(5 * 1_000_000_000)
    assert len(session.drain(clock(), deadline)) == 1


def test_drain_returns_none_at_deadline():
    clock = _manual_clock()
    session = _session({"mdg": []}, clock=clock)
    assert session.drain(clock() + 3600 * 1_000_000_000,
                         clock() + 3600 * 1_000_000_000) is None


def test_queue_events_flow_before_keepalive_deadline():
    clock = _manual_clock()
    session = _session({"mdg": []}, clock=clock)
    session._enqueue(build_envelope("mdg", "quote", 1, {"a": 1}))
    session._enqueue(build_envelope("mdg", "quote", 2, {"a": 2}))
    deadline = clock() + 100 * 1_000_000_000
    f1 = session.drain(clock(), deadline)
    f2 = session.drain(clock(), deadline)
    f3 = session.drain(clock(), deadline)
    assert [json.loads(f[0][5:-2])["data"]["a"] for f in (f1, f2)] == [1, 2]
    assert f3 == []


# -- degraded / recovered, announce-once ------------------------------------

def test_degraded_announced_once_and_recovered_on_return():
    """The exact poll-loop bookkeeping (mark_*_once + announce), driven
    synchronously — the loop's scheduling is covered by the serve_stream
    end-to-end tests."""
    clock = _manual_clock()
    session = _session({"mdg": [SourceTransportError("boom"),
                                SourceTransportError("boom"),
                                [{"seq": 1}]]}, clock=clock)
    src = session.sources[0]
    # poll 1: failure -> degraded announced once
    try:
        src.poll_once()
    except SourceTransportError as e:
        if src.mark_degraded_once():
            session._announce(src.name, "degraded", reason=e.reason)
    # poll 2: failure again -> NO second announcement
    try:
        src.poll_once()
    except SourceTransportError as e:
        if src.mark_degraded_once():
            session._announce(src.name, "degraded", reason=e.reason)
    # poll 3: success -> recovered announced, payload flows through
    payloads = src.poll_once()
    if src.mark_recovered_once():
        session._announce(src.name, "recovered")
    for p in payloads:
        session._enqueue(build_envelope(src.name, src.kind, clock(), p))

    envs = [session._queue.get_nowait() for _ in range(session._queue.qsize())]
    kinds = [(e["src"], e["kind"]) for e in envs]
    assert ("mdg", "degraded") in kinds
    assert kinds.count(("mdg", "degraded")) == 1     # announce once
    assert ("mdg", "recovered") in kinds
    assert kinds.index(("mdg", "degraded")) < kinds.index(("mdg", "recovered"))
    assert session.stats["degraded"] == 1
    degraded = next(e for e in envs if e["kind"] == "degraded")
    assert degraded["data"]["reason"] == "boom"


# -- bounded queue: drop-oldest ---------------------------------------------

def test_queue_full_drops_oldest_not_newest():
    session = _session({}, queue_maxsize=3)
    for i in range(5):
        session._enqueue(build_envelope("x", "k", i, {"i": i}))
    assert session.stats["emitted"] == 5
    assert session.stats["dropped"] == 2
    got = [session._queue.get_nowait()["data"]["i"]
           for _ in range(session._queue.qsize())]
    assert got == [2, 3, 4]  # 0 and 1 dropped, newest kept


# -- serve_stream end-to-end (fake handler, fake transport) ------------------

class _FakeWfile:
    def __init__(self, fail_after_bytes=0):
        self.buf = bytearray()
        self.fail_after_bytes = fail_after_bytes

    def write(self, data):
        if (self.fail_after_bytes and len(self.buf) >= self.fail_after_bytes):
            raise BrokenPipeError("client went away")
        self.buf += data

    def flush(self):
        pass


class _FakeHandler:
    def __init__(self, fail_after_bytes=0):
        self.headers = []
        self.status = None
        self.wfile = _FakeWfile(fail_after_bytes)
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.headers.append((k, v))

    def end_headers(self):
        pass


def _frames(wfile):
    return wfile.buf.split(b"\n\n")


def test_serve_stream_hello_then_events_and_headers():
    from dash.live_sources import Transport

    class Tr(Transport):
        def get(self, base_url, path, timeout_s):
            if path == "/quotes/SYM1?limit=1":
                return {"quotes": [{"seq": 1, "px": 9.0}]}
            return {"count": 0}

    # Wall clock + a tiny max_age_s so the stream ends on its own once the
    # scripted quote has been emitted (the manual clock never advances and
    # would make the deadline unreachable).
    req = StreamRequest(["mdg"], ["SYM1"], 0.2)
    handler = _FakeHandler()
    serve_stream(handler, req, LiveConfig(), transport=Tr(), clock=time.time_ns)
    assert handler.status == 200
    hdrs = dict(handler.headers)
    assert hdrs["Content-Type"] == "text/event-stream"
    assert hdrs["Cache-Control"] == "no-cache"
    assert hdrs["X-Accel-Buffering"] == "no"
    assert handler.close_connection is True
    chunks = [c for c in _frames(handler.wfile) if c.startswith(b"data: ")]
    envs = [json.loads(c[5:]) for c in chunks]
    assert envs[0]["kind"] == "hello"
    assert envs[0]["src"] == "dashboard"
    assert envs[0]["data"]["subscribed"] == ["mdg"]
    quote = next(e for e in envs if e["kind"] == "quote")
    assert quote["src"] == "mdg"
    assert quote["data"]["seq"] == 1


def test_serve_stream_unreachable_source_degrades_and_client_gone_stops():
    from dash.live_sources import Transport

    class Tr(Transport):
        """audl is down; mdg emits a new quote on every poll (steady
        writes so the fake client's broken pipe fires quickly)."""

        def __init__(self):
            self.n = 0

        def get(self, base_url, path, timeout_s):
            if base_url.endswith(":7730"):
                raise SourceTransportError("HTTPConnectionRefused")
            if path.startswith("/quotes/"):
                self.n += 1
                return {"quotes": [{"seq": self.n, "px": 1.0}]}
            return {"count": 0, "events": [], "quotes": []}

    req = StreamRequest(["mdg", "audl"], ["SYM1"], 3600.0)
    handler = _FakeHandler(fail_after_bytes=400)  # dies mid-stream
    serve_stream(handler, req, LiveConfig(), transport=Tr(), clock=time.time_ns)
    envs = [json.loads(c[5:]) for c in _frames(handler.wfile)
            if c.startswith(b"data: ")]
    hello = envs[0]
    assert hello["kind"] == "hello"
    assert hello["data"]["subscribed"] == ["mdg"]   # audl never came up
    assert any(e["kind"] == "degraded" and e["src"] == "audl"
               for e in envs)
    # broken pipe -> pollers stopped before serve_stream returned
    names = {t.name for t in threading.enumerate()}
    assert "live-mdg" not in names
    assert "live-audl" not in names
    assert handler.close_connection is True
