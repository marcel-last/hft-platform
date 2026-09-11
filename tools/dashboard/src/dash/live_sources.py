"""Per-source pollers for the M2 live stream (``GET /live/stream``).

Each live source has one tiny class here. A source owns:

* the endpoint it polls (PLAN.md §3.3 table),
* its dedup state (last ``seq`` / last ``ts`` / seen ids / last breach map),
* a degraded flag with *announce once* semantics.

``Source.poll_once()`` performs exactly one upstream GET (through an
injectable :class:`Transport`) and returns the list of *data payloads* that
are new since the previous poll.  Building the uniform
``{"src","kind","ts","data"}`` envelope and the SSE framing is the
aggregator's job (``dash.live``) — sources never write bytes.

All upstream calls carry an explicit timeout (CONVENTIONS §11).  A poll
failure raises :class:`SourceTransportError`; the caller (poller thread)
decides the degraded/recovered announcement.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Dict, List, Optional


LOGGER_NAME = "dash.live_sources"
logger = logging.getLogger(LOGGER_NAME)

AUDL_EVENT_ID_PREFIX = "EVT-"


class SourceTransportError(Exception):
    """One poll could not be completed (connect/read timeout, bad HTTP,
    bad JSON).  Carries a short ``reason`` for the ``degraded`` event."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Transport:
    """Interface for upstream GETs.  Tests substitute an in-memory fake;
    production uses :class:`HTTPTransport`."""

    def get(self, base_url: str, path: str, timeout_s: float) -> Any:
        raise NotImplementedError


class HTTPTransport(Transport):
    """Stdlib-only GET returning a parsed JSON document (CONVENTIONS §3)."""

    def get(self, base_url: str, path: str, timeout_s: float) -> Any:
        url = base_url + path
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                status = resp.getcode()
                body = resp.read()
        except Exception as e:  # URLError, socket.timeout, HTTPError, ...
            raise SourceTransportError("%s" % type(e).__name__) from e
        if status != 200:
            raise SourceTransportError("HTTP %d" % status)
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise SourceTransportError("bad JSON: %s" % type(e).__name__) from e


class Source:
    """Base class: one endpoint, one dedup state, one degraded flag."""

    def __init__(self, name: str, kind: str, transport: Transport,
                 base_url: str, timeout_s: float, path: str) -> None:
        self.name = name
        self.kind = kind
        self._transport = transport
        self._base_url = base_url
        self._timeout_s = timeout_s
        self._path_str = path
        self._degraded = False

    # -- degraded flag (announce-once semantics) ---------------------------

    def mark_degraded_once(self) -> bool:
        """Return True exactly on the healthy -> degraded transition."""
        if not self._degraded:
            self._degraded = True
            return True
        return False

    def mark_recovered_once(self) -> bool:
        """Return True exactly on the degraded -> healthy transition."""
        if self._degraded:
            self._degraded = False
            return True
        return False

    # -- polling -----------------------------------------------------------

    def poll_once(self) -> List[Dict[str, Any]]:
        """One GET + dedup.  Returns data payloads (possibly empty)."""
        doc = self._transport.get(self._base_url, self._path_str, self._timeout_s)
        if not isinstance(doc, dict):
            raise SourceTransportError("non-object JSON document")
        return self.filter_new(doc)

    def filter_new(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        raise NotImplementedError


class MdgSource(Source):
    """S1 :7610 — ``GET /quotes/{sym}?limit=1`` per symbol (dedup on ``seq``).

    When no symbols were requested the first poll resolves the default set:
    the first 3 canonical symbols from ``GET /symbols`` (PLAN §3.3).
    """

    def __init__(self, transport: Transport, base_url: str, timeout_s: float,
                 symbols: Optional[List[str]] = None) -> None:
        super().__init__("mdg", "quote", transport, base_url, timeout_s, "")
        self._symbols: List[str] = list(symbols or [])
        self._resolved = False
        self._last_seq: Dict[str, int] = {}

    def poll_once(self) -> List[Dict[str, Any]]:
        if not self._resolved:
            if not self._symbols:
                doc = self._transport.get(self._base_url, "/symbols", self._timeout_s)
                rows = (doc or {}).get("symbols") or []
                self._symbols = [
                    r.get("canonical_symbol") for r in rows[:3]
                    if isinstance(r, dict) and r.get("canonical_symbol")
                ]
            self._resolved = True
            if not self._symbols:
                raise SourceTransportError("no symbols available")
        emitted: List[Dict[str, Any]] = []
        for sym in self._symbols:
            doc = self._transport.get(
                self._base_url, "/quotes/%s?limit=1" % sym, self._timeout_s)
            last = self._last_seq.get(sym)
            for q in (doc or {}).get("quotes") or []:
                seq = q.get("seq")
                if not isinstance(seq, int):
                    continue
                if last is None or seq > last:
                    emitted.append(q)
                    self._last_seq[sym] = seq if last is None else max(last, seq)
        return emitted

    # filter_new unused; poll_once handles the per-symbol fan-out.
    def filter_new(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


class ObbSource(Source):
    """S2 :7620 — ``GET /events?limit=100``; the wire event carries a
    monotonically increasing ``ts`` (ns), used as the dedup cursor."""

    def __init__(self, transport: Transport, base_url: str, timeout_s: float) -> None:
        super().__init__("obb", "book-event", transport, base_url, timeout_s,
                         "/events?limit=100")
        self._last_ts = 0

    def filter_new(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for e in doc.get("events") or []:
            ts = e.get("ts")
            if isinstance(ts, int) and ts > self._last_ts:
                self._last_ts = ts
                out.append(e)
        return out


class LatmonSource(Source):
    """S7 :7670 — ``GET /latency``; emit only breach/recovery *transitions*
    of the per-stage ``breached`` flag (never steady-state samples)."""

    def __init__(self, transport: Transport, base_url: str, timeout_s: float) -> None:
        super().__init__("latmon", "latency-breach", transport, base_url,
                         timeout_s, "/latency")
        self._prev: Optional[Dict[str, bool]] = None

    def filter_new(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cur: Dict[str, bool] = {}
        for st in doc.get("stages") or []:
            name = st.get("stage")
            if not isinstance(name, str):
                continue
            breached = bool(st.get("breached", False))
            cur[name] = breached
            if self._prev is not None and self._prev.get(name) is not None \
                    and self._prev[name] != breached:
                row = dict(st)
                row["state"] = "breach" if breached else "recovery"
                out.append(row)
        self._prev = cur
        return out


class AltsvcSource(Source):
    """S10 :7700 — ``GET /alerts/dispatch?limit=100``; dedup on
    ``alert_id`` (an id may reappear in the log on escalation)."""

    MAX_SEEN = 16384

    def __init__(self, transport: Transport, base_url: str, timeout_s: float) -> None:
        super().__init__("altsvc", "alert", transport, base_url, timeout_s,
                         "/alerts/dispatch?limit=100")
        self._seen: Dict[str, None] = {}  # insertion-ordered for boundedness

    def filter_new(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for d in doc.get("dispatches") or []:
            aid = d.get("alert_id")
            if not isinstance(aid, str) or aid in self._seen:
                continue
            self._seen[aid] = None
            out.append(d)
            if len(self._seen) > self.MAX_SEEN:
                # Bounded memory: drop the oldest remembered ids.  A replay
                # of at most one log window after a drop is acceptable for a
                # dashboard stream.
                for k in list(self._seen)[: len(self._seen) - self.MAX_SEEN]:
                    del self._seen[k]
        return out


class CfgsSource(Source):
    """S11 :7710 — long-poll ``GET /changes/watch?timeout_ms=<t>&since=<s>``.

    Every returned change is emitted exactly once; the cursor advances to
    ``latest_seq`` after each successful poll.  The read timeout must
    exceed the upstream watch timeout or the long-poll is cut off
    (aggregator wiring adds a margin — see ``LiveConfig``).
    """

    def __init__(self, transport: Transport, base_url: str, timeout_s: float,
                 watch_timeout_ms: int = 4000) -> None:
        super().__init__("cfgs", "config-change", transport, base_url,
                         timeout_s, "")
        self._watch_ms = watch_timeout_ms
        self._since = 0

    def poll_once(self) -> List[Dict[str, Any]]:
        path = "/changes/watch?timeout_ms=%d&since=%d" % (self._watch_ms, self._since)
        doc = self._transport.get(self._base_url, path, self._timeout_s)
        if not isinstance(doc, dict):
            raise SourceTransportError("non-object JSON document")
        return self.filter_new(doc)

    def filter_new(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        latest = doc.get("latest_seq")
        if isinstance(latest, int) and latest > self._since:
            self._since = latest
        return list(doc.get("events") or [])


class AudlSource(Source):
    """S13 :7730 — ``GET /events?limit=100``; dedup on ``event_id``.

    An ``event_id`` is ``EVT-<12 hex>`` where the hex part is the logical
    sequence, so the cursor is the parsed integer (not a lexicographic
    string comparison).
    """

    def __init__(self, transport: Transport, base_url: str, timeout_s: float) -> None:
        super().__init__("audl", "audit-event", transport, base_url, timeout_s,
                         "/events?limit=100")
        self._last = 0

    @staticmethod
    def _seq_of(event_id: Any) -> Optional[int]:
        if not isinstance(event_id, str) or not event_id.startswith(AUDL_EVENT_ID_PREFIX):
            return None
        body = event_id[len(AUDL_EVENT_ID_PREFIX):]
        try:
            return int(body, 16)
        except ValueError:
            return None

    def filter_new(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for e in doc.get("events") or []:
            seq = self._seq_of(e.get("event_id"))
            if seq is not None and seq > self._last:
                self._last = seq
                out.append(e)
        return out
