"""M2 live stream: aggregator, SSE framing, and the ``/live/stream`` writer.

Threading model (PLAN.md §3.4): ``ThreadingHTTPServer`` already gives one
thread per SSE client; the handler thread *is* the stream (it blocks here).
One daemon poller thread per subscribed source pushes new events into a
bounded :class:`queue.Queue` (``maxsize=1000``, drop-oldest on full) and the
handler thread is the **sole writer** to the socket.  Time comes from an
injectable clock callable so the keepalive cadence is testable without
sleeping (CONVENTIONS §10).

``/live/*`` is a *local* dashboard endpoint (PLAN §3.5): no gateway token,
no S15 hop, and the error path uses the standard UI envelope.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

from dash.errors import UnsupportedParamError
from dash.live_sources import (
    AltsvcSource,
    AudlSource,
    CfgsSource,
    HTTPTransport,
    LatmonSource,
    MdgSource,
    ObbSource,
    Source,
    SourceTransportError,
    Transport,
)


LOGGER_NAME = "dash.live"
logger = logging.getLogger(LOGGER_NAME)

#: The six live sources, in PLAN §3.3 order.  Name -> (package, port, kind).
LIVE_SOURCES: Dict[str, Tuple[int, str]] = {
    "mdg": (7610, "quote"),
    "obb": (7620, "book-event"),
    "latmon": (7670, "latency-breach"),
    "altsvc": (7700, "alert"),
    "cfgs": (7710, "config-change"),
    "audl": (7730, "audit-event"),
}


class LiveConfig:
    """Runtime knobs for one stream (defaults per PLAN §3.2/§3.3)."""

    def __init__(self,
                 host: str = "127.0.0.1",
                 poll_interval_s: float = 2.0,
                 read_timeout_s: float = 1.5,
                 keepalive_interval_s: float = 5.0,
                 max_age_s: float = 3600.0,
                 queue_maxsize: int = 1000) -> None:
        self.host = host
        self.poll_interval_s = poll_interval_s
        self.read_timeout_s = read_timeout_s
        self.keepalive_interval_s = keepalive_interval_s
        self.max_age_s = max_age_s
        self.queue_maxsize = queue_maxsize

    def base_url(self, port: int) -> str:
        return "http://%s:%d" % (self.host, port)


class StreamRequest:
    """Validated input for one ``/live/stream`` request."""

    def __init__(self, sources: List[str], symbols: List[str],
                 max_age_s: float) -> None:
        self.sources = sources
        self.symbols = symbols
        self.max_age_s = max_age_s


def parse_live_query(query: Dict[str, List[str]],
                     cfg: LiveConfig) -> StreamRequest:
    """Validate query params **before** the stream opens (PLAN §3.5).

    * ``sources`` — comma-separated subset; any unknown value -> UI-202.
    * ``symbol``  — repeatable, mdg only.
    * ``max_age_s`` — positive number; bad value -> UI-202.
    """
    raw_sources = query.get("sources") or []
    names: List[str] = []
    for chunk in raw_sources:
        for part in chunk.split(","):
            part = part.strip()
            if part == "":
                continue
            if part not in LIVE_SOURCES:
                raise UnsupportedParamError(
                    "unsupported sources value: %r" % part,
                    context={"param": "sources", "value": part})
            if part not in names:
                names.append(part)
    if not names:
        names = list(LIVE_SOURCES)  # default: all six
    # Emit in PLAN §3.3 order regardless of the user's listing order.
    names = [n for n in LIVE_SOURCES if n in names]

    symbols: List[str] = []
    for sym in (query.get("symbol") or []):
        sym = sym.strip()
        if sym and sym not in symbols:
            symbols.append(sym)

    max_age = cfg.max_age_s
    raw_max_list = query.get("max_age_s") or []
    raw_max = raw_max_list[0] if raw_max_list else ""
    if raw_max is not None and raw_max != "":
        try:
            max_age = float(raw_max)
        except ValueError:
            raise UnsupportedParamError(
                "max_age_s must be a number",
                context={"param": "max_age_s", "value": raw_max})
        if max_age <= 0:
            raise UnsupportedParamError(
                "max_age_s must be > 0",
                context={"param": "max_age_s", "value": raw_max})
    return StreamRequest(names, symbols, max_age)


def build_sources(request: StreamRequest, cfg: LiveConfig,
                  transport: Optional[Transport] = None) -> List[Source]:
    """Instantiate the subscribed sources (order = PLAN §3.3)."""
    tr = transport if transport is not None else HTTPTransport()
    t = cfg.read_timeout_s
    out: List[Source] = []
    for name in request.sources:
        port, _kind = LIVE_SOURCES[name]
        base = cfg.base_url(port)
        if name == "mdg":
            out.append(MdgSource(tr, base, t, symbols=request.symbols))
        elif name == "obb":
            out.append(ObbSource(tr, base, t))
        elif name == "latmon":
            out.append(LatmonSource(tr, base, t))
        elif name == "altsvc":
            out.append(AltsvcSource(tr, base, t))
        elif name == "cfgs":
            # The upstream watch blocks up to 4 s (timeout_ms=4000), so the
            # read timeout must outlive it or every poll would "degrade"
            # (PLAN §3.3; +1 s margin).
            out.append(CfgsSource(tr, base, max(t, 4.0) + 1.0, watch_timeout_ms=4000))
        elif name == "audl":
            out.append(AudlSource(tr, base, t))
    return out


def build_envelope(src: str, kind: str, ts_ns: int,
                   data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The uniform ``{"src","kind","ts","data"}`` event envelope."""
    return {"src": src, "kind": kind, "ts": ts_ns, "data": data if data is not None else {}}


def format_frame(envelope: Dict[str, Any]) -> bytes:
    """One SSE frame: ``data: {json}\\n\\n`` (PLAN §3.2)."""
    return ("data: " + json.dumps(envelope, separators=(",", ":")) + "\n\n").encode("utf-8")


def _now_ns() -> int:
    """int64 nanoseconds since epoch (CONVENTIONS §2)."""
    return time.time_ns()


class LiveSession:
    """One SSE client's aggregation state.

    Owns the bounded outbound queue, the stop flag, the per-source poller
    threads and the reachable-source bookkeeping.  Constructing a session
    starts its pollers; :meth:`drain` is called by the handler thread only.
    """

    def __init__(self, request: StreamRequest, sources: List[Source],
                 cfg: LiveConfig,
                 clock: Optional[Callable[[], int]] = None) -> None:
        self.request = request
        self.sources = sources
        self.cfg = cfg
        self._clock = clock or _now_ns
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(
            maxsize=cfg.queue_maxsize)
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.reachable: List[str] = []  # first-poll outcome, PLAN §3.5
        self._reachable_done = threading.Event()
        self._reachable_count = 0
        self.stats = {"emitted": 0, "dropped": 0, "degraded": 0}
        # Anchor the keepalive timer at session start so the first keepalive
        # is due at start + keepalive_interval_s (not at the first drain call).
        self._last_keepalive_ns: int = self._clock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for src in self.sources:
            t = threading.Thread(
                target=self._poll_loop, args=(src,), daemon=True,
                name="live-%s" % src.name)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        """Stop all pollers and join them (idempotent)."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []

    # -- producer side (poller threads) ------------------------------------

    def _enqueue(self, envelope: Dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            # Drop-oldest: a stalled client must never wedge the pollers
            # (PLAN §3.4).
            try:
                self._queue.get_nowait()
                self.stats["dropped"] += 1
            except queue.Empty:  # pragma: no cover - race-safe
                pass
            try:
                self._queue.put_nowait(envelope)
            except queue.Full:  # pragma: no cover - race-safe
                self.stats["dropped"] += 1
                return
        self.stats["emitted"] += 1

    def _announce(self, src: str, kind: str, reason: str = "") -> None:
        env = build_envelope(src, kind, self._clock(),
                             {"reason": reason} if reason else None)
        self._enqueue(env)
        if kind == "degraded":
            self.stats["degraded"] += 1

    def _poll_loop(self, src: Source) -> None:
        first = True
        while not self._stop.is_set():
            try:
                payloads = src.poll_once()
            except SourceTransportError as e:
                if src.mark_degraded_once():
                    self._announce(src.name, "degraded", reason=e.reason)
                if first:
                    first = False
                    self._first_poll_done(src.name, ok=False)
                self._sleep(self.cfg.poll_interval_s)
                continue
            if first:
                first = False
                self._first_poll_done(src.name, ok=True)
            if src.mark_recovered_once():
                self._announce(src.name, "recovered")
            for payload in payloads:
                env = build_envelope(
                    src.name, src.kind, self._clock(),
                    payload if isinstance(payload, dict) else {"value": payload})
                self._enqueue(env)
            self._sleep(self.cfg.poll_interval_s)

    def _first_poll_done(self, name: str, ok: bool) -> None:
        if ok:
            self.reachable.append(name)
        self._reachable_count += 1
        if self._reachable_count == len(self.sources):
            self._reachable_done.set()

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    # -- consumer side (handler thread, sole writer) ------------------------

    def wait_hello(self) -> bool:
        """Wait for every source's first poll outcome (bounded)."""
        return self._reachable_done.wait(timeout=6.0)

    def drain(self, now_ns: int, deadline_ns: int) -> Optional[List[bytes]]:
        """Return frames to write, or None when the stream must end.

        Emits at most one frame per call when the queue is empty, and at
        most one keepalive per ``keepalive_interval_s`` of *clock* time —
        deterministic under an injected clock (tests never sleep).
        """
        if now_ns >= deadline_ns or self._stop.is_set():
            return None
        if now_ns - self._last_keepalive_ns >= \
                int(self.cfg.keepalive_interval_s * 1_000_000_000):
            self._last_keepalive_ns = now_ns
            return [format_frame(build_envelope("dashboard", "keepalive", now_ns))]
        try:
            env = self._queue.get(timeout=0.05)
        except queue.Empty:
            return []
        return [format_frame(env)]


def _send_frames(handler: Any, frames: List[bytes]) -> None:
    """Write pre-formatted frames to the socket.  The caller catches the
    broken-pipe exceptions; a flush after the batch so the client sees
    progress promptly (streaming, not buffered)."""
    for frame in frames:
        handler.wfile.write(frame)
    handler.wfile.flush()


def serve_stream(handler: Any, request: StreamRequest, cfg: LiveConfig,
                 transport: Optional[Transport] = None,
                 clock: Optional[Callable[[], int]] = None) -> None:
    """Write one complete SSE stream to ``handler`` (the request thread).

    Steps (PLAN §3.2/§3.3/§3.4):

    1. build the subscribed sources, start their poller threads;
    2. wait (bounded) for the first poll outcome of every source;
    3. send SSE headers, then the ``hello`` event with ``subscribed``;
    4. drain frames until the client is gone, ``max_age_s`` elapses, or
       :meth:`LiveSession.drain` says to stop;
    5. stop + join the pollers and mark the connection closed.

    ``handler`` must expose ``send_response/send_header/end_headers``,
    a writable ``wfile`` and a settable ``close_connection`` — i.e. a
    ``BaseHTTPRequestHandler``.
    """
    sources = build_sources(request, cfg, transport)
    session = LiveSession(request, sources, cfg, clock=clock)
    session.start()
    try:
        session.wait_hello()
        hello = build_envelope("dashboard", "hello", session._clock(),
                               {"subscribed": list(session.reachable)})
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("X-Accel-Buffering", "no")
            handler.send_header("Connection", "close")
            handler.close_connection = True
            handler.end_headers()
            _send_frames(handler, [format_frame(hello)])
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("client disconnected before headers/flush")
            return

        now = session._clock()
        deadline_ns = now + int(request.max_age_s * 1_000_000_000)
        while True:
            frames = session.drain(session._clock(), deadline_ns)
            if frames is None:
                break
            if frames:
                try:
                    _send_frames(handler, frames)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    logger.info("client disconnected; stopping %d poller(s)",
                                len(sources))
                    break
    finally:
        session.stop()
        handler.close_connection = True
