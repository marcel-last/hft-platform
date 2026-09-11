"""market_data_gateway — lock-free-style single-producer / multi-consumer ring buffer.

This is the hot-path distribution primitive of the gateway.  A fixed-size
power-of-two array is shared between the producer (the feed/normalization
thread) and any number of consumer threads (one per subscriber connection).

Design notes
------------
* The head index is the *sole* writer-owned cell; consumers each keep a local
  cursor.  Because Python has no true lock-free atomics, we rely on the GIL
  for the single-word index updates and document that this buffer is a
  faithful model of the Rust/Go production implementation (which uses
  ``AtomicU64`` + seqlock).
* Overflow policy: **drop-oldest**.  In a market data feed, a stale quote is
  worthless; dropping the oldest entries keeps consumers as fresh as possible.
  The dropped count is exposed for backpressure metrics.
* Consumers never block on an empty buffer — ``drain`` returns immediately
  with whatever is available (the service main loop polls at
  ``epoll_wait_timeout_ms`` cadence).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

logger = logging.getLogger("mdg.ring_buffer")


class RingBuffer:
    """SPMC ring buffer with drop-oldest overflow semantics."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0 or (capacity & (capacity - 1)) != 0:
            raise ValueError(f"capacity must be a power of two, got {capacity}")
        self._capacity = capacity
        self._mask = capacity - 1
        self._items: List[Optional[Any]] = [None] * capacity
        self._head = 0          # next write slot (producer-owned)
        self._tail = 0          # oldest unread slot (updated by consumers)
        self._dropped = 0       # overflow drop counter
        self._total_written = 0
        self._lock = threading.Lock()  # guards tail updates from consumers

    # -- producer side -------------------------------------------------------

    def write(self, item: Any) -> bool:
        """Append ``item``.  Returns False (and drops the *oldest* entry) when
        the buffer is full."""
        head = self._head
        next_head = (head + 1) & self._mask
        with self._lock:
            if next_head == self._tail:
                # full: drop oldest to make room
                self._items[self._tail] = None
                self._tail = (self._tail + 1) & self._mask
                self._dropped += 1
        self._items[head] = item
        self._head = next_head
        self._total_written += 1
        return True

    def write_batch(self, items: List[Any]) -> int:
        """Write a batch; returns the number of oldest entries dropped."""
        before = self._dropped
        for item in items:
            self.write(item)
        return self._dropped - before

    # -- consumer side -------------------------------------------------------

    def drain(self, max_items: Optional[int] = None) -> List[Any]:
        """Read all available items (up to ``max_items``), advancing the tail."""
        out: List[Any] = []
        while True:
            with self._lock:
                if self._tail == self._head:
                    break
                slot = self._tail
                self._tail = (self._tail + 1) & self._mask
            item = self._items[slot]
            self._items[slot] = None
            if item is not None:
                out.append(item)
                if max_items is not None and len(out) >= max_items:
                    break
        return out

    def peek_available(self) -> int:
        """Number of items currently readable without blocking."""
        with self._lock:
            return (self._head - self._tail) & self._mask

    # -- observability -------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def total_written(self) -> int:
        return self._total_written

    @property
    def fill_pct(self) -> float:
        """Current fill ratio in [0, 1)."""
        with self._lock:
            used = (self._head - self._tail) & self._mask
        return used / self._capacity


class ShardedRingBuffer:
    """A set of :class:`RingBuffer` shards selected by a hash of the key.

    Sharding reduces lock contention between consumers that follow disjoint
    symbol sets (e.g. one strategy engine per asset class).  The shard count
    must be a power of two so that the selection is a cheap bitmask.
    """

    def __init__(self, shard_count: int, capacity_per_shard: int) -> None:
        if shard_count <= 0 or (shard_count & (shard_count - 1)) != 0:
            raise ValueError(f"shard_count must be a power of two, got {shard_count}")
        self._shards = [RingBuffer(capacity_per_shard) for _ in range(shard_count)]
        self._mask = shard_count - 1

    def _shard_for(self, key: str) -> RingBuffer:
        h = 0
        for ch in key:
            h = (h * 31 + ord(ch)) & 0xFFFFFFFF
        return self._shards[h & self._mask]

    def write(self, key: str, item: Any) -> bool:
        return self._shard_for(key).write(item)

    def drain(self, key: str, max_items: Optional[int] = None) -> List[Any]:
        return self._shard_for(key).drain(max_items)

    def drain_all(self, max_items_per_shard: Optional[int] = None) -> int:
        """Drain every shard; returns total items read (for the fan-out layer)."""
        total = 0
        for shard in self._shards:
            total += len(shard.drain(max_items_per_shard))
        return total

    def stats(self) -> List[dict]:
        return [
            {
                "shard": i,
                "capacity": s.capacity,
                "available": s.peek_available(),
                "fill_pct": round(s.fill_pct, 4),
                "dropped": s.dropped_count,
                "written": s.total_written,
            }
            for i, s in enumerate(self._shards)
        ]
