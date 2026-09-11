"""portfolio_analytics — analytics core.

The :class:`AnalyticsEngine` is the heart of S9.  On each refresh pass it:

1.  ingests one S6 ``/positions`` listing (authoritative positions with
    realized P&L and, when available, mark-to-market values);
2.  aggregates per-symbol realized + unrealized P&L into :class:`PnlRow`s;
3.  appends a point-in-time equity reading to the bounded rolling series;
4.  recomputes performance metrics (Sharpe, drawdown, win rate) and risk
    metrics (historical / parametric VaR + CVaR) over that series;
5.  derives per-symbol attribution of total P&L.

The equity basis is **cash-neutral**: ``equity = sum(realized_pnl) +
sum(unrealized_pnl)`` across all positions, so a flat book reads zero and the
return series tracks trading performance rather than notional size.  Returns
are simple period-over-period changes in that equity value; Sharpe is
annualized from the per-period mean / stdev using the configured annualization
basis.  VaR is reported as an expected *loss* (positive number) at the
configured confidence level.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Any, Dict, List, Optional, Sequence

from .config import CONFIG, ServiceConfig
from .models import (
    AttributionRow,
    EquitySample,
    PnlRow,
    PortfolioMetrics,
    PositionRow,
    VarResult,
    now_ns,
)

logger = logging.getLogger("pfa.analytics_engine")


# ---------------------------------------------------------------------------
# Pure numeric helpers (module-level so they are trivially unit-testable)
# ---------------------------------------------------------------------------

def _mean(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def _stdev_population(xs: Sequence[float]) -> float:
    """Population standard deviation (divide by N). Returns 0.0 for <2 samples."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    return math.sqrt(var)


def _sharpe(returns: Sequence[float], risk_free_annual: float,
            periods_per_year: int) -> Optional[float]:
    """Annualized Sharpe ratio. Returns None when there are fewer than 2 returns."""
    n = len(returns)
    if n < 2:
        return None
    rf_per_period = risk_free_annual / periods_per_year
    excess = [r - rf_per_period for r in returns]
    m = _mean(excess)
    sd = _stdev_population(excess)
    if sd == 0.0:
        # Zero variance: a deterministic edge is unbounded Sharpe; report None
        # to avoid emitting an infinite ratio that would break JSON consumers.
        return None
    return (m / sd) * math.sqrt(periods_per_year)


def _max_drawdown_pct(equity: Sequence[float]) -> float:
    """Worst peak-to-trough decline as a negative percentage over the series."""
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak * 100.0
            if dd < worst:
                worst = dd
    return worst


def _current_drawdown_pct(equity: Sequence[float]) -> float:
    """Drawdown from the running peak to the latest value, as a negative percent."""
    if not equity:
        return 0.0
    peak = max(equity)
    last = equity[-1]
    if peak <= 0:
        return 0.0
    return (last - peak) / peak * 100.0


def _win_rate_pct(returns: Sequence[float]) -> Optional[float]:
    """Percentage of strictly positive returns; None when there are no returns."""
    n = len(returns)
    if n == 0:
        return None
    wins = sum(1 for r in returns if r > 0.0)
    return wins / n * 100.0


def _annualized_return(equity: Sequence[float], periods_per_year: int) -> Optional[float]:
    """Compound annualized return from first to last equity value.

    Returns None when the series is shorter than two points or the starting
    value is non-positive (compounding is undefined).
    """
    if len(equity) < 2:
        return None
    start = equity[0]
    end = equity[-1]
    if start <= 0:
        return None
    periods = len(equity) - 1
    ratio = end / start
    # Guard against a negative ratio (equity crossed zero) which has no real root.
    if ratio <= 0:
        return None
    years = periods / periods_per_year
    if years <= 0:
        return None
    # Compounding over a sub-second window extrapolates to an absurd horizon and
    # can overflow; cap the implied annualization at one year so the figure stays
    # meaningful (and finite) for fast refresh cadences.
    if years < 1.0:
        years = 1.0
    return math.pow(ratio, 1.0 / years) - 1.0


def _historical_var(returns: Sequence[float], confidence: float) -> tuple[Optional[float], Optional[float]]:
    """Historical VaR and CVaR as expected *losses* (positive numbers).

    Returns ``(None, None)`` when the series is empty.  The loss at level
    ``confidence`` is the negated return at the ``(1 - confidence)`` quantile
    of the sorted returns (lower-tail linear interpolation).  CVaR is the mean
    loss over all observations at or below that threshold.
    """
    if not returns:
        return None, None
    srt = sorted(returns)
    n = len(srt)
    alpha = 1.0 - confidence
    # Index of the lower tail boundary (fractional).
    idx = alpha * (n - 1)
    lo = int(math.floor(idx))
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    q_ret = srt[lo] * (1.0 - frac) + srt[hi] * frac
    var_loss = -q_ret

    # CVaR: mean loss over the worst observations (those <= quantile return).
    tail = [r for r in srt if r <= q_ret]
    if not tail:
        tail = [srt[0]]
    cvar_loss = -(_mean(tail))
    return var_loss, cvar_loss


def _parametric_var(returns: Sequence[float], confidence: float) -> tuple[Optional[float], Optional[float]]:
    """Parametric (normal) VaR and CVaR as expected *losses* (positive numbers).

    Uses the per-period mean and population stdev of the returns.  Returns
    ``(None, None)`` when there are fewer than two samples (stdev undefined).
    ``z`` is the standard-normal quantile at level ``confidence`` via an
    Acklam-style rational approximation (stdlib only).
    """
    n = len(returns)
    if n < 2:
        return None, None
    m = _mean(returns)
    sd = _stdev_population(returns)
    z = _norm_ppf(confidence)
    var_loss = -(m - z * sd)

    # CVaR under the normal model: mean loss beyond the VaR threshold.
    if sd == 0.0:
        cvar_loss = var_loss
    else:
        phi_cdf_z = _norm_cdf(z)              # P(Z <= z) = confidence
        tail_prob = max(1e-12, 1.0 - phi_cdf_z)
        pdf_at_z = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        cvar_loss = -(m + sd * (pdf_at_z / tail_prob))
    return var_loss, cvar_loss


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (rational approximation), stdlib only."""
    # Clamp to avoid the poles of the approximation.
    p = min(max(p, 1e-9), 1.0 - 1e-9)
    if p < 0.5:
        return -_norm_ppf(1.0 - p)
    t = math.sqrt(-2.0 * math.log(1.0 - p))
    # Coefficients (Acklam / Winitzki-style); accurate to ~1e-7.
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t)


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF via the error function (math.erf, stdlib)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class AnalyticsEngine:
    """Thread-safe portfolio analytics engine over the S6 position book."""

    def __init__(self, cfg: Optional[ServiceConfig] = None) -> None:
        self.cfg = cfg or CONFIG
        self._lock = threading.Lock()
        self._positions: Dict[str, PositionRow] = {}
        self._equity_series: List[EquitySample] = []
        # lifetime counters
        self.refreshes_completed = 0
        self.upstream_failures = 0
        self.last_refresh_ns = 0

    # ------------------------------------------------------------------
    # Ingestion (the core pass)
    # ------------------------------------------------------------------

    def ingest_positions(self, raw_rows: Optional[Sequence[Dict[str, Any]]]) -> int:
        """Ingest one S6 ``/positions`` listing; returns the number of positions.

        A ``None`` or empty input is treated as "no data this pass" — it does
        NOT wipe existing state, so a transient S6 outage never zeroes the book
        or corrupts the equity series.  Callers increment ``upstream_failures``
        separately when the pull itself raised.
        """
        if not raw_rows:
            return 0

        rows: List[PositionRow] = []
        for raw in raw_rows:
            if isinstance(raw, dict):
                try:
                    rows.append(PositionRow.from_dict(raw))
                except (ValueError, TypeError) as exc:  # noqa: BLE001 - skip malformed
                    logger.debug("skipping malformed S6 position row: %s", exc)

        if not rows:
            return 0

        with self._lock:
            # Replace the position map wholesale (authoritative snapshot).
            self._positions = {r.symbol: r for r in rows}
            self._append_equity_sample_locked()
            self.refreshes_completed += 1
            self.last_refresh_ns = now_ns()
        return len(rows)

    def _append_equity_sample_locked(self) -> None:
        """Compute and append one equity sample (call with lock held)."""
        realized = sum(r.realized_pnl for r in self._positions.values())
        unrealized = sum(r.effective_unrealized for r in self._positions.values())
        market_value = sum(abs(r.net_qty) * r.mark for r in self._positions.values())
        equity = realized + unrealized
        self._equity_series.append(EquitySample(
            ts_ns=now_ns(),
            equity=equity,
            market_value=market_value,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
        ))
        cap = self.cfg.history.max_history_points
        if len(self._equity_series) > cap:
            del self._equity_series[: len(self._equity_series) - cap]

    # ------------------------------------------------------------------
    # P&L views
    # ------------------------------------------------------------------

    def _pnl_rows_locked(self) -> List[PnlRow]:
        rows: List[PnlRow] = []
        for symbol in sorted(self._positions):
            p = self._positions[symbol]
            mv = p.effective_market_value
            un = p.effective_unrealized
            rows.append(PnlRow(
                symbol=p.symbol,
                account=p.account,
                net_qty=p.net_qty,
                side=p.side,
                avg_price=p.avg_price,
                mark=p.mark,
                market_value=mv,
                realized_pnl=p.realized_pnl,
                unrealized_pnl=un,
                total_pnl=p.realized_pnl + un,
            ))
        return rows

    def pnl(self) -> Dict[str, Any]:
        """Aggregate P&L across all symbols with per-symbol breakdown."""
        with self._lock:
            rows = self._pnl_rows_locked()
        total_realized = sum(r.realized_pnl for r in rows)
        total_unrealized = sum(r.unrealized_pnl for r in rows)
        total_market_value = sum(abs(r.market_value) for r in rows)
        return {
            "ts_ns": now_ns(),
            "symbols_total": len(rows),
            "totals": {
                "realized_pnl": round(total_realized, 4),
                "unrealized_pnl": round(total_unrealized, 4),
                "total_pnl": round(total_realized + total_unrealized, 4),
                "market_value": round(total_market_value, 4),
            },
            "symbols": [r.to_dict() for r in rows],
        }

    def pnl_symbol(self, symbol: str) -> Optional[PnlRow]:
        """Return one symbol's P&L row (None if the symbol is unknown)."""
        with self._lock:
            p = self._positions.get(symbol)
            if p is None:
                return None
            mv = p.effective_market_value
            un = p.effective_unrealized
            return PnlRow(
                symbol=p.symbol,
                account=p.account,
                net_qty=p.net_qty,
                side=p.side,
                avg_price=p.avg_price,
                mark=p.mark,
                market_value=mv,
                realized_pnl=p.realized_pnl,
                unrealized_pnl=un,
                total_pnl=p.realized_pnl + un,
            )

    # ------------------------------------------------------------------
    # Attribution
    # ------------------------------------------------------------------

    def attribution(self) -> Dict[str, Any]:
        """Per-symbol decomposition of total P&L into realized/unrealized parts."""
        with self._lock:
            rows = self._pnl_rows_locked()
        grand_total = sum(r.total_pnl for r in rows)
        denom = abs(grand_total) if grand_total != 0 else 0.0
        out: List[AttributionRow] = []
        for r in rows:
            weight = (abs(r.total_pnl) / denom * 100.0) if denom > 0 else 0.0
            out.append(AttributionRow(
                symbol=r.symbol,
                realized=r.realized_pnl,
                unrealized=r.unrealized_pnl,
                total=r.total_pnl,
                weight_pct=weight,
            ))
        # Largest absolute contributor first, then by symbol.
        out.sort(key=lambda a: (-abs(a.total), a.symbol))
        return {
            "ts_ns": now_ns(),
            "grand_total": round(grand_total, 4),
            "realized_total": round(sum(r.realized_pnl for r in rows), 4),
            "unrealized_total": round(sum(r.unrealized_pnl for r in rows), 4),
            "symbols": [a.to_dict() for a in out],
        }

    # ------------------------------------------------------------------
    # Performance & risk metrics
    # ------------------------------------------------------------------

    def _returns_locked(self) -> List[float]:
        eq = [s.equity for s in self._equity_series]
        rets: List[float] = []
        for i in range(1, len(eq)):
            prev = eq[i - 1]
            if prev == 0.0:
                # No base to measure a change against; treat as a flat period.
                rets.append(0.0)
            else:
                rets.append((eq[i] - prev) / abs(prev))
        return rets

    def metrics(self) -> PortfolioMetrics:
        """Compute rolling performance metrics over the equity/return series."""
        with self._lock:
            eq = [s.equity for s in self._equity_series]
            rets = self._returns_locked()

        mt = self.cfg.metrics
        sharpe = _sharpe(rets, mt.risk_free_annual, mt.periods_per_day)
        if sharpe is not None and len(rets) < mt.min_returns_for_sharpe:
            sharpe = None

        annualized = _annualized_return(eq, mt.periods_per_day)
        return PortfolioMetrics(
            samples=len(eq),
            returns=len(rets),
            sharpe_ratio=sharpe,
            max_drawdown_pct=_max_drawdown_pct(eq),
            current_drawdown_pct=_current_drawdown_pct(eq),
            win_rate_pct=_win_rate_pct(rets),
            mean_return=_mean(rets),
            stdev_return=_stdev_population(rets),
            annualized_return=annualized,
        )

    def var(self, method: Optional[str] = None, confidence: Optional[float] = None) -> VarResult:
        """Compute VaR / CVaR over the return series.

        ``method`` and ``confidence`` override the configured defaults when
        provided (validated loosely here; config validation is authoritative at
        boot).  Returns a :class:`VarResult` with ``insufficient_data=True``
        when the series is shorter than ``history.min_samples_for_var``.
        """
        method = method or self.cfg.risk.method
        conf = confidence if confidence is not None else self.cfg.risk.confidence
        need = self.cfg.history.min_samples_for_var

        with self._lock:
            rets = self._returns_locked()

        if len(rets) < need:
            return VarResult(
                method=method,
                confidence=conf,
                samples=len(rets),
                var=None,
                cvar=None,
                insufficient_data=True,
            )

        if method == "parametric":
            v, cv = _parametric_var(rets, conf)
        else:
            v, cv = _historical_var(rets, conf)
        return VarResult(
            method=method,
            confidence=conf,
            samples=len(rets),
            var=v,
            cvar=cv,
            insufficient_data=False,
        )

    # ------------------------------------------------------------------
    # History & stats
    # ------------------------------------------------------------------

    def history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Bounded equity/return series, newest first."""
        with self._lock:
            samples = list(reversed(self._equity_series))
        return [s.to_dict() for s in samples[:limit]]

    def readiness_reasons(self) -> List[str]:
        """Reasons the service is not ready (empty list = ready)."""
        reasons: List[str] = []
        if self.refreshes_completed == 0:
            reasons.append("no refresh pass completed yet")
        return reasons

    def stats_view_locked(self) -> Dict[str, Any]:
        """Engine counters + position count (call with lock held)."""
        eq = [s.equity for s in self._equity_series]
        rets = self._returns_locked()
        realized = sum(r.realized_pnl for r in self._positions.values())
        unrealized = sum(r.effective_unrealized for r in self._positions.values())
        return {
            "positions_tracked": len(self._positions),
            "equity_samples": len(eq),
            "returns_observed": len(rets),
            "current_equity": round(realized + unrealized, 4),
            "realized_pnl": round(realized, 4),
            "unrealized_pnl": round(unrealized, 4),
            "refreshes_completed": self.refreshes_completed,
            "upstream_failures": self.upstream_failures,
            "last_refresh_ns": self.last_refresh_ns,
        }

    def stats_view(self) -> Dict[str, Any]:
        with self._lock:
            return self.stats_view_locked()
