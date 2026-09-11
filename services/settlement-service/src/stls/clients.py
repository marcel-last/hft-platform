"""Outbound HTTP clients for the settlement service (S14).

S14 has exactly one upstream dependency: the position keeper (S6).  The
ingest loop pulls ``GET /positions`` on a fixed cadence and converts the
snapshot into settlement fills via
:meth:`stls.settlement_engine.SettlementEngine.ingest_from_positions`.

Rules (CONVENTIONS §11):

* stdlib :mod:`urllib.request` only, explicit timeouts;
* every failure is swallowed and counted — the ingest loop must never crash
  the service because S6 is briefly unreachable;
* no retries inside the client (the next pull is the retry).
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional
from urllib.request import Request, urlopen

logger = logging.getLogger("stls.clients")


class PositionKeeperClient:
    """Minimal client for S6 position-keeper's ``GET /positions``."""

    def __init__(self, base_url: str, connect_timeout_ms: int = 500,
                 read_timeout_s: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout_ms = connect_timeout_ms
        self.read_timeout_s = read_timeout_s
        self.pull_failures = 0
        self.pull_successes = 0
        #: Consecutive failures since the last success; 0 while healthy.
        #: readyz keys off this (a single transient S6 blip must not strand
        #: the service in not_ready forever; the LATEST pull decides).
        self.consecutive_failures = 0

    def pull_positions(self) -> Optional[List[dict]]:
        """Return the position rows, or ``None`` on any failure (counted)."""
        url = f"{self.base_url}/positions"
        req = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": "settlement-service/1.0"})
        try:
            with urlopen(req, timeout=self.read_timeout_s) as resp:
                if resp.status != 200:
                    self._record_failure()
                    logger.warning("S6 /positions returned HTTP %s", resp.status)
                    return None
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - any network/JSON failure is a counted miss
            self._record_failure()
            logger.debug("S6 /positions pull failed: %r", exc)
            return None
        rows = payload.get("positions") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            self._record_failure()
            logger.warning("S6 /positions payload has no 'positions' array")
            return None
        self.pull_successes += 1
        self.consecutive_failures = 0
        return rows

    def _record_failure(self) -> None:
        self.pull_failures += 1
        self.consecutive_failures += 1
