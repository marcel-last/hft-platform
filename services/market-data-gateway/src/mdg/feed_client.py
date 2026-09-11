"""market_data_gateway — venue feed client (transport + protocol layer).

This module implements the network layer that talks to the three venue feeds
declared in :class:`~mdg.config.FeedConfig`.  It is written against a small,
explicit transport abstraction so that the real socket implementation and the
deterministic mock replay implementation share the same state machine.

State machine per venue connection::

    DISCONNECTED -> CONNECTING -> AUTHENTICATING -> SUBSCRIBING -> STREAMING
         ^                                                        |
         |                                                        v
         +---------------- RECONNECTING <-------------- SUSPECT --+
                                |
                                v (after N consecutive failures)
                              DEAD

The client is event-driven: ``poll()`` is called by the service main loop and
returns a batch of raw messages plus control events.  All timestamps are
stamped at receive time so that downstream latency accounting is exact.
"""

from __future__ import annotations

import logging
import random
import socket
import struct
from typing import Callable, Dict, List, Optional, Tuple

from .config import CONFIG, VenueFeed
from .errors import (
    FeedAuthError,
    FeedConnectionError,
    FeedProtocolError,
    FeedTimeoutError,
)
from .models import (
    ControlEvent,
    ControlEventKind,
    QuoteAction,
    RawHeartbeat,
    RawQuote,
    RawTrade,
    Side,
    VenueStatus,
    now_ns,
)

logger = logging.getLogger("mdg.feed_client")


# ---------------------------------------------------------------------------
# Transport abstraction
# ---------------------------------------------------------------------------

class Transport:
    """Minimal byte-stream transport interface.

    Real deployments use :class:`SocketTransport`; tests and the offline mock
    pipeline use :class:`~mdg.mock_feed.MockVenueTransport`.
    """

    def connect(self, host: str, port: int) -> None: ...
    def send(self, payload: bytes) -> None: ...
    def recv(self, max_bytes: int) -> bytes: ...
    def close(self) -> None: ...

    def set_blocking(self) -> None:
        """Switch the transport from handshake (timeout) mode to blocking
        streaming mode.  Transports that do not support this are no-ops."""
        return None


class SocketTransport(Transport):
    """Blocking TCP transport with tuned socket options."""

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._sock: Optional[socket.socket] = None

    def connect(self, host: str, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._cfg.recv_buffer_bytes)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self._cfg.send_buffer_bytes)
        if self._cfg.tcp_nodelay:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ka = (self._cfg.keepalive_idle_seconds,
              self._cfg.keepalive_interval_seconds,
              self._cfg.keepalive_probes)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(self._cfg.handshake_timeout_ms / 1000.0)
        try:
            sock.connect((host, port))
        except OSError as exc:
            sock.close()
            raise FeedConnectionError(
                f"tcp connect to {host}:{port} failed", context={"host": host, "port": port, "reason": str(exc)}
            ) from exc
        self._sock = sock

    def send(self, payload: bytes) -> None:
        if self._sock is None:
            raise FeedConnectionError("send on closed transport")
        try:
            self._sock.sendall(payload)
        except OSError as exc:
            raise FeedConnectionError(f"tcp send failed: {exc}") from exc

    def recv(self, max_bytes: int) -> bytes:
        if self._sock is None:
            raise FeedConnectionError("recv on closed transport")
        try:
            return self._sock.recv(max_bytes)
        except socket.timeout as exc:
            raise FeedTimeoutError(f"tcp recv timed out after {self._cfg.handshake_timeout_ms} ms") from exc

    def set_blocking(self) -> None:
        if self._sock is not None:
            self._sock.settimeout(None)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._sock = None


# ---------------------------------------------------------------------------
# Wire framing (ITCH-style 4-byte length prefix, little-endian)
# ---------------------------------------------------------------------------

def frame_encode(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def frame_decode(buf: bytearray) -> List[bytes]:
    """Pull all complete frames out of ``buf`` (mutating it)."""
    frames: List[bytes] = []
    while len(buf) >= 4:
        (length,) = struct.unpack_from("<I", buf, 0)
        if length > 1_048_576:  # 1 MiB sanity cap per frame
            raise FeedProtocolError("transport", f"frame length {length} exceeds 1 MiB cap")
        if len(buf) < 4 + length:
            break
        frames.append(bytes(buf[4:4 + length]))
        del buf[: 4 + length]
    return frames


# ---------------------------------------------------------------------------
# Per-venue connection state machine
# ---------------------------------------------------------------------------

class VenueConnection:
    """Owns one venue transport and its reconnection backoff."""

    def __init__(self, venue: VenueFeed, transport_factory: Callable[[], Transport]) -> None:
        self.venue = venue
        self._transport_factory = transport_factory
        self.status: VenueStatus = VenueStatus.DISCONNECTED
        self._transport: Optional[Transport] = None
        self._buffer = bytearray()
        self._consecutive_failures = 0
        self._next_reconnect_delay_ms = CONFIG.network.reconnect_base_delay_ms
        self._last_heartbeat_ns = now_ns()
        self.heartbeat_misses = 0
        self.messages_received = 0
        self.bytes_received = 0

    # -- lifecycle -----------------------------------------------------------

    @staticmethod
    def _recv_frames(transport: Transport, max_bytes: int) -> List[bytes]:
        """Read from the transport and decode all complete frames.

        Used for handshake replies (AUTH-OK / SUB-OK), which are also sent as
        length-prefixed frames by the venue.  If no complete frame is present
        in the first read, retry up to three times with a short sleep so that
        slow or partial deliveries still complete the handshake.
        """
        import time as _time
        buf = bytearray()
        for _attempt in range(3):
            chunk = transport.recv(max_bytes)
            if chunk:
                buf.extend(chunk)
            frames = frame_decode(buf)
            if frames:
                return frames
            _time.sleep(0.01)
        return frame_decode(buf)

    def try_connect(self) -> bool:
        """Attempt the full connect->auth->subscribe handshake.

        Returns True when the connection reached STREAMING.
        """
        self.status = VenueStatus.CONNECTING
        transport = self._transport_factory()
        try:
            transport.connect(self.venue.host, self.venue.port)
        except FeedConnectionError:
            self._register_failure()
            return False

        self.status = VenueStatus.AUTHENTICATING
        auth_payload = (
            f"AUTH|user={self.venue.username}|proto={self.venue.protocol}\n".encode("ascii")
        )
        try:
            transport.send(auth_payload)
            ack_frames = self._recv_frames(transport, 4096)
        except FeedConnectionError:
            self._register_failure()
            return False

        if not ack_frames or not ack_frames[0].startswith(b"AUTH-OK"):
            transport.close()
            raw = ack_frames[0] if ack_frames else b""
            raise FeedAuthError(
                f"venue {self.venue.venue_id} rejected authentication",
                context={"venue_id": self.venue.venue_id, "raw_ack": raw[:128].decode("utf-8", "replace")},
            )

        self.status = VenueStatus.SUBSCRIBING
        sub_lines = [f"SUB|{s}\n" for s in self.venue.symbols]
        transport.send("".join(sub_lines).encode("ascii"))
        try:
            sub_frames = self._recv_frames(transport, 4096)
        except FeedConnectionError:
            self._register_failure()
            return False
        if not sub_frames or not sub_frames[0].startswith(b"SUB-OK"):
            transport.close()
            raise FeedProtocolError(self.venue.venue_id, "subscription rejected", None)

        # switch from handshake-timeout mode to blocking streaming mode so
        # data-phase recv() calls wait for venue traffic instead of timing
        # out after the (short) handshake budget
        transport.set_blocking()
        self._transport = transport
        self.status = VenueStatus.STREAMING
        self._consecutive_failures = 0
        self._next_reconnect_delay_ms = CONFIG.network.reconnect_base_delay_ms
        self._last_heartbeat_ns = now_ns()
        return True

    def _register_failure(self) -> None:
        self._consecutive_failures += 1
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self.status = VenueStatus.RECONNECTING
        # exponential backoff with jitter, capped at the configured maximum
        base = min(
            CONFIG.network.reconnect_max_delay_ms,
            self._next_reconnect_delay_ms * 2,
        )
        jitter_pct = CONFIG.network.reconnect_jitter_pct
        jitter = int(base * (jitter_pct / 100.0) * (random.random() * 2 - 1))
        self._next_reconnect_delay_ms = max(
            CONFIG.network.reconnect_base_delay_ms, base + jitter
        )

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self.status = VenueStatus.DISCONNECTED

    # -- data path -----------------------------------------------------------

    def poll(self) -> Tuple[List[object], List[ControlEvent]]:
        """Drain one batch of raw messages from the venue stream.

        Returns ``(raw_messages, control_events)``.  A heartbeat message is
        surfaced as a :class:`RawHeartbeat`; quote/trade bytes are parsed by
        :meth:`FeedClient._parse_payload` in the owning client.
        """
        events: List[ControlEvent] = []
        if self.status != VenueStatus.STREAMING or self._transport is None:
            return [], events

        try:
            chunk = self._transport.recv(CONFIG.network.recv_buffer_bytes)
        except FeedTimeoutError:
            # idle timeout: check heartbeat budget before declaring suspect
            now = now_ns()
            since_hb_ms = (now - self._last_heartbeat_ns) / 1_000_000.0
            if since_hb_ms > CONFIG.quality.heartbeat_interval_ms * (
                CONFIG.quality.heartbeat_miss_limit + 1
            ):
                self.status = VenueStatus.SUSPECT
                events.append(ControlEvent(
                    kind=ControlEventKind.HEARTBEAT_MISSED,
                    venue_id=self.venue.venue_id,
                    detail=f"no heartbeat for {since_hb_ms:.0f} ms",
                ))
            return [], events
        except FeedConnectionError as exc:
            self._register_failure()
            events.append(ControlEvent(
                kind=ControlEventKind.VENUE_DISCONNECTED,
                venue_id=self.venue.venue_id,
                detail=str(exc),
            ))
            return [], events

        if not chunk:
            # peer closed the connection cleanly
            self.status = VenueStatus.DISCONNECTED
            self._transport.close()
            self._transport = None
            events.append(ControlEvent(
                kind=ControlEventKind.VENUE_DISCONNECTED,
                venue_id=self.venue.venue_id,
                detail="peer closed connection",
            ))
            return [], events

        self.bytes_received += len(chunk)
        self._buffer.extend(chunk)

        try:
            frames = frame_decode(self._buffer)
        except FeedProtocolError as exc:
            logger.error("protocol error on %s: %s", self.venue.venue_id, exc.message)
            self.close()
            events.append(ControlEvent(
                kind=ControlEventKind.VENUE_DISCONNECTED,
                venue_id=self.venue.venue_id,
                detail=f"protocol violation: {exc.message}",
            ))
            return [], events

        messages: List[object] = []
        for frame in frames:
            parsed = self._parse_frame(frame)
            if parsed is None:
                continue
            kind, payload = parsed
            if kind == "HB":
                self._last_heartbeat_ns = now_ns()
                self.heartbeat_misses = 0
                messages.append(RawHeartbeat(
                    venue_id=self.venue.venue_id,
                    seq_no=int(payload.get("seq", 0) or 0),
                    receive_timestamp_ns=now_ns(),
                ))
            elif kind == "Q":
                try:
                    messages.append(self._parse_quote_frame(payload))
                except (KeyError, ValueError) as exc:
                    raise FeedProtocolError(
                        self.venue.venue_id,
                        f"malformed quote frame: {exc}",
                        int(payload.get("seq", 0) or 0),
                    ) from exc
            elif kind == "T":
                try:
                    messages.append(self._parse_trade_frame(payload))
                except (KeyError, ValueError) as exc:
                    raise FeedProtocolError(
                        self.venue.venue_id,
                        f"malformed trade frame: {exc}",
                        int(payload.get("seq", 0) or 0),
                    ) from exc
            else:
                # unknown frame kind: count it and move on (forward compat)
                logger.debug("ignoring unknown frame kind %r on %s", kind, self.venue.venue_id)
            self.messages_received += 1
        return messages, events

    # -- typed frame parsers --------------------------------------------------

    @staticmethod
    def _side(value: str) -> Side:
        try:
            return Side(value)
        except ValueError as exc:
            raise ValueError(f"unknown side {value!r}") from exc

    @staticmethod
    def _action(value: str) -> QuoteAction:
        try:
            return QuoteAction(value)
        except ValueError as exc:
            raise ValueError(f"unknown action {value!r}") from exc

    def _parse_quote_frame(self, fields: Dict[str, str]) -> RawQuote:
        """Parse a ``Q|...`` quote frame into a typed :class:`RawQuote`."""
        return RawQuote(
            venue_id=self.venue.venue_id,
            symbol_venue=fields["sym"],
            seq_no=int(fields["seq"]),
            msg_type=self._action(fields["act"]),
            side=self._side(fields["side"]),
            price=float(fields["px"]),
            quantity=int(fields["qty"]),
            depth_level=int(fields.get("lvl", 1)),
            venue_timestamp_ns=int(fields.get("vt", 0)),
            receive_timestamp_ns=now_ns(),
        )

    def _parse_trade_frame(self, fields: Dict[str, str]) -> RawTrade:
        """Parse a ``T|...`` trade frame into a typed :class:`RawTrade`."""
        return RawTrade(
            venue_id=self.venue.venue_id,
            symbol_venue=fields["sym"],
            seq_no=int(fields["seq"]),
            price=float(fields["px"]),
            quantity=int(fields["qty"]),
            aggressor_side=self._side(fields.get("aggr", "BID")),
            exec_id=fields.get("xid", f"{self.venue.venue_id}-{fields['seq']}"),
            venue_timestamp_ns=int(fields.get("vt", 0)),
            receive_timestamp_ns=now_ns(),
        )

    def _parse_frame(self, frame: bytes) -> Optional[Tuple[str, dict]]:
        """Parse one venue frame into (kind, payload-dict).

        The mock protocol uses a simple ``TYPE|k=v|k=v`` text encoding; the
        production ITCH/MDP3 adapters would replace this single method.
        """
        try:
            text = frame.decode("ascii")
        except UnicodeDecodeError as exc:
            raise FeedProtocolError(self.venue.venue_id, f"non-ascii frame: {exc}") from exc
        parts = text.split("|")
        if not parts:
            return None
        kind = parts[0]
        fields: Dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                fields[k] = v
        return kind, fields


# ---------------------------------------------------------------------------
# Feed client (owns all venue connections)
# ---------------------------------------------------------------------------

class FeedClient:
    """Top-level feed client: owns every :class:`VenueConnection`."""

    def __init__(self, transport_factory: Optional[Callable[[], Transport]] = None) -> None:
        self._transport_factory = transport_factory or (lambda: SocketTransport(CONFIG.network))
        self.connections: Dict[str, VenueConnection] = {
            venue.venue_id: VenueConnection(venue, self._transport_factory)
            for venue in CONFIG.feeds.venues
        }

    def connect_all(self) -> List[ControlEvent]:
        """Attempt to bring every venue feed up.  Returns control events."""
        events: List[ControlEvent] = []
        # highest priority first so that a partial outage still covers the
        # most important venues as quickly as possible
        ordered = sorted(self.connections.values(), key=lambda c: c.venue.priority)
        for conn in ordered:
            try:
                ok = conn.try_connect()
            except FeedAuthError:
                conn.status = VenueStatus.DEAD
                events.append(ControlEvent(
                    kind=ControlEventKind.VENUE_DISCONNECTED,
                    venue_id=conn.venue.venue_id,
                    detail="authentication failed; connection marked DEAD",
                ))
                continue
            if ok:
                events.append(ControlEvent(
                    kind=ControlEventKind.VENUE_CONNECTED,
                    venue_id=conn.venue.venue_id,
                    detail=f"streaming {len(conn.venue.symbols)} symbols via {conn.venue.protocol}",
                ))
        return events

    def poll_all(self) -> Tuple[List[object], List[ControlEvent]]:
        """Poll every streaming connection; auto-reconnect suspects."""
        messages: List[object] = []
        events: List[ControlEvent] = []
        for conn in self.connections.values():
            if conn.status == VenueStatus.SUSPECT or (
                conn.status == VenueStatus.RECONNECTING and conn._consecutive_failures > 0
            ):
                # attempt a reconnect cycle
                try:
                    if conn.try_connect():
                        events.append(ControlEvent(
                            kind=ControlEventKind.VENUE_CONNECTED,
                            venue_id=conn.venue.venue_id,
                            detail="reconnected after suspect period",
                        ))
                except FeedAuthError:
                    conn.status = VenueStatus.DEAD
                    events.append(ControlEvent(
                        kind=ControlEventKind.CIRCUIT_BREAKER_OPEN,
                        venue_id=conn.venue.venue_id,
                        detail="auth failure during reconnect; breaker open",
                    ))
                continue
            batch, evts = conn.poll()
            messages.extend(batch)
            events.extend(evts)
        return messages, events

    def status_report(self) -> Dict[str, dict]:
        """Health summary for the /healthz and /feeds endpoints."""
        report: Dict[str, dict] = {}
        for venue_id, conn in self.connections.items():
            report[venue_id] = {
                "status": conn.status.value,
                "protocol": conn.venue.protocol,
                "symbols": list(conn.venue.symbols),
                "priority": conn.venue.priority,
                "messages_received": conn.messages_received,
                "bytes_received": conn.bytes_received,
                "consecutive_failures": conn._consecutive_failures,
                "next_reconnect_delay_ms": conn._next_reconnect_delay_ms,
            }
        return report
