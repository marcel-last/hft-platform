"""Domain models and wire (de)serialization for the settlement service.

All timestamps on hot paths are int64 nanoseconds since Unix epoch (§2).
``canonical_json`` / ``content_hash`` give EOD runs a tamper-evident fingerprint:
a finalized report's ``content_hash`` is recomputable by anyone from the JSON
representation alone (the same property S13 gives its audit chain).

The Clock pattern (System/Manual) makes every time-dependent behaviour —
"today", date floors, finalization stamps — deterministic in tests without
sleeping.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .errors import InvalidDateParam, InvalidSettleRequest


def now_ns() -> int:
    """Current Unix time in nanoseconds (wall clock)."""
    from time import time_ns
    return time_ns()


# ---------------------------------------------------------------------------
# Clock pattern
# ---------------------------------------------------------------------------

class Clock:
    """Abstract monotonic-ish time source used by the engine."""

    def now_ns(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def today_date(self) -> str:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(self.now_ns() / 1e9).strftime("%Y-%m-%d")


class SystemClock(Clock):
    """Production clock backed by the OS wall time."""

    def now_ns(self) -> int:
        from time import time_ns
        return time_ns()


class ManualClock(Clock):
    """Test clock with a fixed, manually advanced nanosecond counter."""

    def __init__(self, start_ns: Optional[int] = None) -> None:
        self._ns = start_ns if start_ns is not None else 1_700_000_000_000_000_000

    def now_ns(self) -> int:
        return self._ns

    def set(self, ns: int) -> None:
        self._ns = int(ns)

    def advance_ns(self, delta: int) -> None:
        if delta < 0:
            raise ValueError("advance_ns requires a non-negative delta")
        self._ns += int(delta)


# ---------------------------------------------------------------------------
# Settlement dates
# ---------------------------------------------------------------------------

_DATE_FLOOR_YEAR = 1970

def parse_settlement_date(raw: Any, clock: Clock) -> str:
    """Validate and normalize a settlement-date string (``YYYY-MM-DD``).

    Raises ``InvalidDateParam`` (STL-202) with the offending value in context.
    The date must not be before 1970 and must not lie strictly after today —
    an operator cannot pre-settle a day that has not happened yet.
    """
    import datetime as _dt

    if not isinstance(raw, str):
        raise InvalidDateParam("Settlement date must be a string in YYYY-MM-DD form.",
                               context={"field": "date", "raw": repr(raw)})
    value = raw.strip()
    try:
        parsed = _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidDateParam(
            f"Settlement date is not a valid ISO calendar date: {exc}",
            context={"field": "date", "raw": value}) from None
    if parsed.year < _DATE_FLOOR_YEAR:
        raise InvalidDateParam("Settlement date predates 1970.",
                               context={"field": "date", "raw": value})
    today = clock.today_date()
    if value > today:
        # Lexicographic comparison is safe for ISO dates of fixed width (YYYY-MM-DD).
        raise InvalidDateParam("Settlement date must not be in the future.",
                               context={"field": "date", "raw": value, "today": today})
    return parsed.isoformat()


def normalize_date_param(raw: Any, clock: Clock) -> str:
    """Parse a query-string ``?date=`` parameter through :func:`parse_settlement_date`."""
    if raw is None or (isinstance(raw, str) and not raw):
        raise InvalidDateParam("The date query parameter is required.", context={"field": "date"})
    return parse_settlement_date(raw, clock)


# ---------------------------------------------------------------------------
# Canonical JSON + hashing (tamper-evidence for finalized EOD runs)
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """Key-sorted compact JSON with ASCII escaping — the hash basis."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(obj: Any) -> str:
    """SHA-256 hex digest of :func:`canonical_json` over ``obj``."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fills (what we believe happened, sourced from S4 via S6)
# ---------------------------------------------------------------------------

_FILL_SIDES = ("BUY", "SELL")

@dataclass(frozen=True)
class FillRecord:
    """One internal fill as recorded for settlement purposes."""

    fill_id: str
    ts_ns: int
    venue: str
    symbol: str
    side: str          # BUY or SELL (aggressor side)
    px: float
    qty: int
    fee: float         # commission/fees charged against this fill
    account: str

    @property
    def notional(self) -> float:
        return self.px * self.qty


def parse_fill(obj: Any, index: int) -> FillRecord:
    """Parse and validate one element of the settle body's ``fills`` array (STL-201 on failure)."""
    where = {"field": f"fills[{index}]"}
    if not isinstance(obj, dict):
        raise InvalidSettleRequest(f"Fills entry {index} must be a JSON object.", context=where)

    fill_id = obj.get("fill_id")
    if not isinstance(fill_id, str) or not (1 <= len(fill_id) <= 128):
        raise InvalidSettleRequest(
            f"fills[{index}].fill_id must be a non-empty string of at most 128 chars.",
            context={**where, "raw": repr(fill_id)[:200]})

    ts_ns = obj.get("ts_ns")
    if not isinstance(ts_ns, int) or isinstance(ts_ns, bool) or ts_ns < 0:
        raise InvalidSettleRequest(
            f"fills[{index}].ts_ns must be a non-negative integer (ns since epoch).",
            context={**where, "raw": repr(ts_ns)[:200]})

    venue = obj.get("venue")
    if not isinstance(venue, str) or not (1 <= len(venue) <= 64):
        raise InvalidSettleRequest(f"fills[{index}].venue must be a non-empty string.", context=where)

    symbol = obj.get("symbol")
    if not isinstance(symbol, str) or not (1 <= len(symbol) <= 64):
        raise InvalidSettleRequest(f"fills[{index}].symbol must be a non-empty string.", context=where)

    side_raw = obj.get("side", "BUY")
    side = side_raw.upper() if isinstance(side_raw, str) else ""
    if side not in _FILL_SIDES:
        raise InvalidSettleRequest(
            f"fills[{index}].side must be one of {list(_FILL_SIDES)} (case-insensitive).",
            context={**where, "raw": repr(side_raw)[:200]})

    px = obj.get("px")
    if isinstance(px, bool) or not isinstance(px, (int, float)) or px < 0:
        raise InvalidSettleRequest(f"fills[{index}].px must be a non-negative number.", context=where)
    qty = obj.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise InvalidSettleRequest(f"fills[{index}].qty must be a positive integer.", context=where)

    fee = obj.get("fee", 0.0)
    if isinstance(fee, bool) or not isinstance(fee, (int, float)) or fee < 0:
        raise InvalidSettleRequest(f"fills[{index}].fee must be a non-negative number.", context=where)

    account = obj.get("account", "MAIN")
    if not isinstance(account, str) or not (1 <= len(account) <= 64):
        raise InvalidSettleRequest(f"fills[{index}].account must be a string of at most 64 chars.",
                                   context=where)

    return FillRecord(fill_id, int(ts_ns), venue.upper(), symbol.upper(), side,
                      float(px), int(qty), float(fee), account.upper())


# ---------------------------------------------------------------------------
# Venue statement lines (what the venue says happened)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatementLine:
    """One line from a venue's end-of-day (or intraday) trade confirmation statement."""

    stmt_line_id: str
    ts_ns: Optional[int]   # venue stamp, if present; None is tolerated for legacy statements
    venue: str
    symbol: str
    side: str              # BUY or SELL from the *venue's* perspective of our fill
    qty: int
    px: float
    fee: float             # commission shown on the statement line

    @property
    def notional(self) -> float:
        return self.px * self.qty


def parse_statement_line(obj: Any, index: int, seq: int = 0) -> StatementLine:
    """Parse and validate one element of the settle body's ``statement_lines`` array (STL-201)."""
    where = {"field": f"statement_lines[{index}]"}
    if not isinstance(obj, dict):
        raise InvalidSettleRequest(f"Statement line {index} must be a JSON object.", context=where)

    venue = obj.get("venue")
    if not isinstance(venue, str) or not (1 <= len(venue) <= 64):
        raise InvalidSettleRequest(f"statement_lines[{index}].venue must be a non-empty string.",
                                   context=where)
    symbol = obj.get("symbol")
    if not isinstance(symbol, str) or not (1 <= len(symbol) <= 64):
        raise InvalidSettleRequest(f"statement_lines[{index}].symbol must be a non-empty string.",
                                   context=where)

    side_raw = obj.get("side", "BUY")
    side = side_raw.upper() if isinstance(side_raw, str) else ""
    if side not in _FILL_SIDES:
        raise InvalidSettleRequest(
            f"statement_lines[{index}].side must be one of {list(_FILL_SIDES)} (case-insensitive).",
            context={**where, "raw": repr(side_raw)[:200]})

    px = obj.get("px")
    if isinstance(px, bool) or not isinstance(px, (int, float)) or px < 0:
        raise InvalidSettleRequest(f"statement_lines[{index}].px must be a non-negative number.", context=where)
    qty = obj.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise InvalidSettleRequest(f"statement_lines[{index}].qty must be a positive integer.", context=where)

    fee = obj.get("fee", 0.0)
    if isinstance(fee, bool) or not isinstance(fee, (int, float)) or fee < 0:
        raise InvalidSettleRequest(f"statement_lines[{index}].fee must be a non-negative number.", context=where)

    ts_ns = obj.get("ts_ns", None)
    if ts_ns is not None and (isinstance(ts_ns, bool) or not isinstance(ts_ns, int) or ts_ns < 0):
        raise InvalidSettleRequest(
            f"statement_lines[{index}].ts_ns must be null or a non-negative integer.", context=where)

    line_id = obj.get("stmt_line_id")
    if line_id is None:
        line_id = f"STM-{venue.upper()}-{seq}"
    if not isinstance(line_id, str) or not (1 <= len(line_id) <= 128):
        raise InvalidSettleRequest(f"statement_lines[{index}].stmt_line_id must be a string of at most 128 chars.",
                                   context=where)

    return StatementLine(line_id, int(ts_ns) if ts_ns is not None else None,
                         venue.upper(), symbol.upper(), side, int(qty), float(px), float(fee))


# ---------------------------------------------------------------------------
# Discrepancies (flagged for manual review)
# ---------------------------------------------------------------------------

DISCREPANCY_KINDS = ("QTY_MISMATCH", "PRICE_MISMATCH", "MISSING_STATEMENT", "UNEXPECTED_SYMBOL")
_SEVERITY_BY_KIND = {
    "QTY_MISMATCH": "CRITICAL",        # quantity that cannot be reconciled is never cosmetic
    "PRICE_MISMATCH": "WARNING",       # price within a larger band, or venue fee drift
    "MISSING_STATEMENT": "CRITICAL",   # we have fills the venue statement does not cover at all
    "UNEXPECTED_SYMBOL": "WARNING",    # the venue reported activity we do not hold locally
}

@dataclass(frozen=True)
class Discrepancy:
    """One reconciliation finding for a (date, venue, symbol), flagged for manual review."""

    kind: str                    # one of DISCREPANCY_KINDS
    date: str
    venue: str
    symbol: str
    our_qty: int                 # 0 when we hold nothing locally
    stmt_qty: int                # 0 when the statement reports nothing
    our_notional: float          # average-price notional on our side (BUY+SELL totals)
    stmt_notional: float         # px*qty sum from statement lines for this symbol
    our_avg_px: Optional[float]  # None when either qty side is zero
    stmt_avg_px: Optional[float]
    detected_ns: int

    @property
    def severity(self) -> str:
        return _SEVERITY_BY_KIND[self.kind]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "date": self.date,
            "venue": self.venue,
            "symbol": self.symbol,
            "our_qty": self.our_qty,
            "stmt_qty": self.stmt_qty,
            "qty_delta": self.our_qty - self.stmt_qty,
            "our_notional": round(self.our_notional, 6),
            "stmt_notional": round(self.stmt_notional, 6),
            "our_avg_px": None if self.our_avg_px is None else round(self.our_avg_px, 10),
            "stmt_avg_px": None if self.stmt_avg_px is None else round(self.stmt_avg_px, 10),
            "detected_ns": self.detected_ns,
        }


# ---------------------------------------------------------------------------
# Settlement runs (one per settlement date)
# ---------------------------------------------------------------------------

@dataclass
class SettlementRun:
    """Mutable reconciliation state for one calendar day.

    Intraday mode accepts new fills and replaces statement lines wholesale per
    venue; after ``finalize_eod()`` the run is sealed with a content hash over
    its canonical snapshot and rejects further mutations (STL-206).
    """

    date: str
    kind: str = "intraday"          # intraday | eod
    finalized: bool = False
    created_ns: int = 0
    updated_ns: int = 0
    settle_calls: int = 0
    duplicate_fills_ignored: int = 0
    fills: List[FillRecord] = field(default_factory=list)
    statement_lines: List[StatementLine] = field(default_factory=list)
    content_hash: Optional[str] = None
    _fill_digests: Dict[str, str] = field(default_factory=dict)

    def seal_snapshot(self) -> Dict[str, Any]:
        """The canonical object a content hash is computed over (engine uses this)."""
        return {
            "date": self.date,
            "fills": [f.__dict__ for f in self.fills],
            "statement_lines": [l.__dict__ for l in self.statement_lines],
        }
