"""Part 1 of 2 — reconciliation core for the settlement service (S14).

Matching model
--------------

For one settlement run the engine compares, per ``(venue, symbol)``:

* the **internal** fills S4/S6 recorded (BUY = +qty, SELL = -qty), and
* the **venue statement** lines the counterparty reported.

A pair reconciles cleanly when both the net-quantity delta and the
average-price delta fall inside the tolerances from
:mod:`stls.config.MatchingConfig`:

* ``|our_net_qty - stmt_net_qty| <= qty_tolerance`` — otherwise
  ``QTY_MISMATCH`` (CRITICAL).
* ``|our_avg_px - stmt_avg_px| <= price_tolerance_abs`` **and** no statement
  line price deviates from ours beyond the price tolerance — otherwise
  ``PRICE_MISMATCH`` (WARNING).

A venue/symbol we hold fills for with **no** statement lines at all becomes a
``MISSING_STATEMENT`` (CRITICAL).  A venue/symbol present only on the
statement side becomes ``UNEXPECTED_SYMBOL`` (WARNING).

All matching math is plain float arithmetic with explicit tolerances; there is
no fixed-point requirement here (§2 applies to latency paths, and settlement
is an EOD accounting surface).  Averages are notional-weighted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .models import Discrepancy, FillRecord, StatementLine


@dataclass(frozen=True)
class SideTotals:
    """Aggregated one-sided totals for a single (venue, symbol)."""

    net_qty: int            # BUY +qty, SELL -qty
    gross_qty: int          # sum of |qty| over all lines (turnover volume)
    notional: float         # sum of px*qty (signed by side)
    absolute_notional: float  # sum of px*qty (unsigned, turnover value)
    avg_px: float           # weighted average fill price (absolute-price basis)
    line_count: int


def _avg_from(signed_notional: float, gross_qty: int) -> float:
    """Notional-weighted average price on an absolute (unsigned) basis."""
    if gross_qty <= 0:
        return 0.0
    # signed_notional may be negative for net-short books; the *price* is a
    # positive average, so we work on the unsigned turnover.
    return abs(signed_notional) / gross_qty


def aggregate_fills(fills: List[FillRecord], venue: str, symbol: str) -> SideTotals:
    """Aggregate the run's internal fills for one (venue, symbol)."""
    net_qty = 0
    gross_qty = 0
    signed_notional = 0.0
    count = 0
    for fill in fills:
        if fill.venue != venue or fill.symbol != symbol:
            continue
        signed = fill.qty if fill.side == "BUY" else -fill.qty
        net_qty += signed
        gross_qty += fill.qty
        signed_notional += (fill.px * fill.qty) if fill.side == "BUY" else -(fill.px * fill.qty)
        count += 1
    return SideTotals(net_qty, gross_qty, signed_notional,
                      abs(signed_notional), _avg_from(signed_notional, gross_qty), count)


def aggregate_statement(lines: List[StatementLine], venue: str, symbol: str) -> SideTotals:
    """Aggregate venue statement lines for one (venue, symbol).

    Statement ``side`` is the aggressor side of *our* fill as the venue reports
    it, i.e. the same convention as internal fills: BUY adds, SELL subtracts.
    """
    net_qty = 0
    gross_qty = 0
    signed_notional = 0.0
    count = 0
    for line in lines:
        if line.venue != venue or line.symbol != symbol:
            continue
        signed = line.qty if line.side == "BUY" else -line.qty
        net_qty += signed
        gross_qty += line.qty
        signed_notional += (line.px * line.qty) if line.side == "BUY" else -(line.px * line.qty)
        count += 1
    return SideTotals(net_qty, gross_qty, signed_notional,
                      abs(signed_notional), _avg_from(signed_notional, gross_qty), count)


def keys_with_fills(fills: List[FillRecord]) -> List[Tuple[str, str]]:
    """Sorted set of (venue, symbol) pairs present among internal fills."""
    return sorted({(f.venue, f.symbol) for f in fills})


def keys_with_statement(lines: List[StatementLine]) -> List[Tuple[str, str]]:
    """Sorted set of (venue, symbol) pairs present among statement lines."""
    return sorted({(l.venue, l.symbol) for l in lines})


def matching_report(
    fills: List[FillRecord],
    lines: List[StatementLine],
    date: str,
    detected_ns: int,
    qty_tolerance: int,
    price_tolerance_abs: float,
) -> List[Discrepancy]:
    """Reconcile every (venue, symbol) seen on either side; return discrepancies.

    The result is ordered deterministically by (venue, symbol, kind) so EOD
    reports and the ``/discrepancies`` endpoint are stable across re-runs on
    the same data.
    """
    out: List[Discrepancy] = []
    ours = {key: aggregate_fills(fills, *key) for key in keys_with_fills(fills)}
    theirs = {key: aggregate_statement(lines, *key) for key in keys_with_statement(lines)}

    for key in sorted(set(ours) | set(theirs)):
        venue, symbol = key
        o = ours.get(key)
        t = theirs.get(key)

        if o is not None and t is None:
            out.append(Discrepancy(
                kind="MISSING_STATEMENT", date=date, venue=venue, symbol=symbol,
                our_qty=o.net_qty, stmt_qty=0,
                our_notional=o.notional, stmt_notional=0.0,
                our_avg_px=o.avg_px if o.gross_qty > 0 else None, stmt_avg_px=None,
                detected_ns=detected_ns))
            continue
        if o is None and t is not None:
            out.append(Discrepancy(
                kind="UNEXPECTED_SYMBOL", date=date, venue=venue, symbol=symbol,
                our_qty=0, stmt_qty=t.net_qty,
                our_notional=0.0, stmt_notional=t.notional,
                our_avg_px=None, stmt_avg_px=t.avg_px if t.gross_qty > 0 else None,
                detected_ns=detected_ns))
            continue

        # Both sides present: quantity first, then price.
        qty_delta = abs(o.net_qty - t.net_qty)
        if qty_delta > qty_tolerance:
            out.append(Discrepancy(
                kind="QTY_MISMATCH", date=date, venue=venue, symbol=symbol,
                our_qty=o.net_qty, stmt_qty=t.net_qty,
                our_notional=o.notional, stmt_notional=t.notional,
                our_avg_px=o.avg_px if o.gross_qty > 0 else None,
                stmt_avg_px=t.avg_px if t.gross_qty > 0 else None,
                detected_ns=detected_ns))
            continue

        price_delta = abs(o.avg_px - t.avg_px)
        if price_delta > price_tolerance_abs:
            out.append(Discrepancy(
                kind="PRICE_MISMATCH", date=date, venue=venue, symbol=symbol,
                our_qty=o.net_qty, stmt_qty=t.net_qty,
                our_notional=o.notional, stmt_notional=t.notional,
                our_avg_px=o.avg_px if o.gross_qty > 0 else None,
                stmt_avg_px=t.avg_px if t.gross_qty > 0 else None,
                detected_ns=detected_ns))

    out.sort(key=lambda d: (d.venue, d.symbol, d.kind))
    return out
