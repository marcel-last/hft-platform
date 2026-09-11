"""market_data_gateway — deterministic mock venue feed.

This module provides an offline, fully deterministic replay of venue traffic
so that the entire platform can be exercised end-to-end without live venue
access.  It implements the :class:`~mdg.feed_client.Transport` interface and
generates:

* a realistic random-walk price process per symbol (seeded, reproducible),
* L2 depth updates with bid/ask levels that respect the tick grid,
* occasional sequence gaps and out-of-order retransmissions (configurable
  rates) so the quality monitor has real work to do,
* heartbeats at the configured cadence.

The generator is *not* fast — it exists for correctness, replay and testing,
never for latency-sensitive paths.
"""

from __future__ import annotations

import random
import struct
import threading
from typing import Dict, List, Optional, Tuple

from .config import CONFIG
from .feed_client import Transport, frame_encode
from .models import QuoteAction, Side


class _SymbolSim:
    """Random-walk state for one venue symbol."""

    def __init__(self, venue_id: str, symbol: str, tick_size: float, start_price: float) -> None:
        self.venue_id = venue_id
        self.symbol = symbol
        self.tick_size = tick_size
        # price in ticks (integer lattice) — keeps everything on-grid by construction
        self.price_ticks = int(round(start_price / tick_size))
        self.bid_qty = 10
        self.ask_qty = 10
        self.seq_no = 0

    def step(self, rng: random.Random) -> Tuple[float, float]:
        """Advance the walk by one tick move; returns (bid, ask)."""
        # 70% no-move, 30% one-tick move in a random direction (slight mean reversion)
        if rng.random() < 0.30:
            drift = -0.15 * (self.price_ticks - int(round(
                self._anchor / self.tick_size))) if hasattr(self, "_anchor") else 0
            direction = rng.choice([-1, 1])
            if rng.random() < max(0.0, min(1.0, 0.5 + drift * 0.01)):
                direction = -direction
            self.price_ticks += direction
        # quantities breathe
        self.bid_qty = max(1, self.bid_qty + rng.choice([-2, -1, 0, 1, 2]))
        self.ask_qty = max(1, self.ask_qty + rng.choice([-2, -1, 0, 1, 2]))
        mid = self.price_ticks * self.tick_size
        bid = mid - self.tick_size
        ask = mid + self.tick_size
        return bid, ask

    def _next_seq(self) -> int:
        self.seq_no += 1
        return self.seq_no


class MockVenueTransport(Transport):
    """Deterministic mock transport implementing the venue wire protocol.

    Usage::

        t = MockVenueTransport(seed=42, gap_rate=0.005, ooo_rate=0.002)
        t.connect("mock", 0)          # no-op
        t.send(auth_payload)          # -> AUTH-OK frame
        t.send(sub_payload)           # -> SUB-OK frame
        data = t.recv(65536)          # -> next batch of frames

    Each ``recv`` call produces a small batch (1-8 messages) so that the
    gateway's framing/decoding path is exercised realistically.
    """

    def __init__(
        self,
        seed: int = 42,
        gap_rate: float = 0.005,
        ooo_rate: float = 0.002,
        messages_per_recv: int = 8,
        heartbeat_every: int = 100,
    ) -> None:
        self._rng = random.Random(seed)
        self._gap_rate = gap_rate
        self._ooo_rate = ooo_rate
        self._messages_per_recv = messages_per_recv
        self._heartbeat_every = max(1, heartbeat_every)
        self._msgs_since_hb = 0
        self._connected = False
        self._authed = False
        self._subbed = False
        self._sims: Dict[str, _SymbolSim] = {}
        self._ooo_pending: List[bytes] = []   # frames to replay out of order
        self._pending_ack: Optional[bytes] = None  # handshake ack buffered for next recv

    # -- Transport interface -------------------------------------------------

    def connect(self, host: str, port: int) -> None:
        self._connected = True

    def send(self, payload: bytes) -> None:
        text = payload.decode("ascii", "replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("AUTH|") and not self._authed:
                self._authed = True
                self._pending_ack = frame_encode(b"AUTH-OK|mock")
            elif line.startswith("SUB|"):
                symbol = line[4:].strip()
                sim = self._make_sim(symbol)
                if sim is not None:
                    self._sims[symbol] = sim
                self._subbed = True
        # a subscription batch completes when every SUB line has been seen;
        # buffer the ack so the next recv returns exactly one logical reply
        if self._authed and self._subbed and self._pending_ack is None:
            self._pending_ack = frame_encode(f"SUB-OK|symbols={len(self._sims)}".encode("ascii"))

    def recv(self, max_bytes: int) -> bytes:
        if not self._connected:
            return b""
        if self._pending_ack is not None:
            ack, self._pending_ack = self._pending_ack, None
            return ack

        out = bytearray()
        for _ in range(self._messages_per_recv):
            frame = self._next_message()
            if frame is None:
                break
            # sequence gap simulation: drop the frame (gap of 1)
            if self._rng.random() < self._gap_rate:
                continue
            # out-of-order simulation: stash this frame and replay an older one
            if self._rng.random() < self._ooo_rate and len(self._ooo_pending) > 0:
                out.extend(frame_encode(self._ooo_pending.pop(0)))
                self._ooo_pending.append(frame)
                continue
            self._ooo_pending.append(frame)
            if len(self._ooo_pending) > 64:
                self._ooo_pending.pop(0)
            out.extend(frame_encode(frame))

        # heartbeat cadence
        self._msgs_since_hb += 1
        if self._msgs_since_hb >= self._heartbeat_every:
            self._msgs_since_hb = 0
            hb = f"HB|seq={self._global_seq()}"
            out.extend(frame_encode(hb.encode("ascii")))

        return bytes(out[:max_bytes])

    def close(self) -> None:
        self._connected = False

    # -- internals -----------------------------------------------------------

    def _make_sim(self, symbol: str) -> Optional[_SymbolSim]:
        # find the canonical tick size for this venue symbol
        for (venue, vsym), canonical in CONFIG.normalization.canonical_map.items():
            if vsym == symbol and canonical in CONFIG.normalization.tick_sizes:
                tick = CONFIG.normalization.tick_sizes[canonical][0]
                sim = _SymbolSim(venue, symbol, tick, self._rng.uniform(100.0, 5000.0))
                sim._anchor = sim.price_ticks * tick
                return sim
        return None

    def _global_seq(self) -> int:
        max_seq = 0
        for sim in self._sims.values():
            max_seq = max(max_seq, sim.seq_no)
        return max_seq + 1

    def _next_message(self) -> Optional[bytes]:
        if not self._sims:
            return None
        symbol = self._rng.choice(list(self._sims.keys()))
        sim = self._sims[symbol]
        bid, ask = sim.step(self._rng)
        seq = sim._next_seq()
        # emit a random side/level update to keep the book shape varied
        side = Side.BID if self._rng.random() < 0.5 else Side.ASK
        price = bid if side is Side.BID else ask
        qty = sim.bid_qty if side is Side.BID else sim.ask_qty
        level = self._rng.choice([1, 1, 1, 2, 3])
        action = QuoteAction.MODIFY if self._rng.random() < 0.8 else QuoteAction.NEW
        payload = (
            f"Q|sym={sim.symbol}|seq={seq}|act={action.value}|side={side.value}"
            f"|px={price:.6f}|qty={qty}|lvl={level}|vt={_mock_ns()}"
        )
        return payload.encode("ascii")


def _mock_ns() -> int:
    """Monotonic-ish mock venue clock (nanoseconds)."""
    import time
    return time.time_ns()
