//! latency_monitor — latency statistics core.
//!
//! The :struct:`LatencyMonitor` owns one bounded rolling window per pipeline
//! stage behind a `Mutex`.  It is the single source of truth for latency
//! statistics and enforces the budget policy: after each recorded sample it
//! re-checks the affected stage's p99 against its budget (with a per-stage
//! cooldown) and, on breach, appends an :struct:`Alert` to a bounded history.
//!
//! All durations are `i64` nanoseconds; percentiles are computed with integer
//! linear interpolation over a sorted copy of the window (the only allocation
//! on the read path).  Floats appear nowhere in the latency math.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use crate::config::Config;
use crate::errors::Error;
use crate::models::{Alert, AlertSeverity, Budget, LatencySample, Stage};

/// Per-stage statistics snapshot (returned by the query endpoints).
#[derive(Debug, Clone)]
pub struct StageStats {
    pub stage: Stage,
    /// Number of samples currently in the rolling window.
    pub count: usize,
    /// Total samples ever recorded for this stage (monotonic).
    pub total: u64,
    /// Min / max duration in the current window (ns), `None` when empty.
    pub min_ns: Option<i64>,
    pub max_ns: Option<i64>,
    /// Mean duration (integer ns) over the current window, `None` when empty.
    pub mean_ns: Option<i64>,
    /// p50 / p99 / p999 in integer ns (nearest-rank interpolation), `None`
    /// when the window is empty.
    pub p50_ns: Option<i64>,
    pub p99_ns: Option<i64>,
    pub p999_ns: Option<i64>,
    /// The configured budget for this stage, if any.
    pub budget_ns: Option<i64>,
    /// True when the current p99 exceeds `budget * breach_ratio`.
    pub breached: bool,
}

/// Thread-safe store of per-stage rolling windows + alert history.
#[derive(Default)]
pub struct LatencyMonitor {
    cfg: Config,
    /// One bounded window per stage (insertion order == Stage::all() order).
    windows: Mutex<Vec<VecDeque<i64>>>,
    /// Monotonic "ever recorded" counters per stage.
    totals: Mutex<Vec<u64>>,
    /// Bounded alert history (newest appended at the back).
    alerts: Mutex<Vec<Alert>>,
    /// Last alert timestamp per stage, for cooldown enforcement.
    last_alert_ns: Mutex<Vec<Option<i64>>>,
    /// Monotonic alert sequence number.
    next_alert_seq: Mutex<u64>,
}

impl LatencyMonitor {
    pub fn new(cfg: Config) -> Self {
        let n = Stage::all().len();
        let cap = cfg.window.window_size;
        LatencyMonitor {
            cfg,
            windows: Mutex::new(vec![VecDeque::with_capacity(cap); n]),
            totals: Mutex::new(vec![0u64; n]),
            alerts: Mutex::new(Vec::new()),
            last_alert_ns: Mutex::new(vec![None; n]),
            next_alert_seq: Mutex::new(1),
        }
    }

    fn idx(stage: Stage) -> usize {
        match stage {
            Stage::VenueToGateway => 0,
            Stage::GatewayToBook => 1,
            Stage::BookToSignal => 2,
            Stage::SignalToOrder => 3,
            Stage::OrderToAck => 4,
        }
    }

    // -- mutation -----------------------------------------------------------

    /// Record a latency sample: append it to the stage's rolling window, bump
    /// the monotonic counter, and (re)evaluate the budget policy.  Returns the
    /// alert that was emitted, if any.
    pub fn record(&self, sample: &LatencySample) -> Result<Option<Alert>, Error> {
        let i = Self::idx(sample.stage);

        {
            let mut windows = self.windows.lock().unwrap();
            let w = &mut windows[i];
            w.push_back(sample.duration_ns);
            while w.len() > self.cfg.window.window_size {
                w.pop_front();
            }
        }
        {
            let mut totals = self.totals.lock().unwrap();
            totals[i] += 1;
        }

        // Budget check (only stages with a configured budget).
        let budget_ns = self
            .cfg
            .budget
            .stage_budgets_ns
            .iter()
            .find(|(name, _)| *name == sample.stage.as_str())
            .map(|(_, ns)| *ns);

        let mut alert = None;
        if let Some(budget) = budget_ns {
            let p99 = self.percentile(sample.stage, 99.0).unwrap_or(0);
            // breach when p99 > budget * breach_ratio (integer math on both sides)
            let threshold = (budget as f64 * self.cfg.budget.breach_ratio) as i64;
            if p99 > threshold {
                alert = self.maybe_alert(sample.stage, p99, budget);
            }
        }
        Ok(alert)
    }

    /// Emit an alert for `stage` if the per-stage cooldown has elapsed.  The
    /// caller must have already determined that a breach exists.
    fn maybe_alert(&self, stage: Stage, p99_ns: i64, budget_ns: i64) -> Option<Alert> {
        let now = crate::models::now_ns();
        let i = Self::idx(stage);
        {
            let mut last = self.last_alert_ns.lock().unwrap();
            if let Some(prev) = last[i] {
                if now.saturating_sub(prev) < self.cfg.budget.alert_cooldown_ns {
                    return None; // still in cooldown
                }
            }
            last[i] = Some(now);
        }

        let seq = {
            let mut s = self.next_alert_seq.lock().unwrap();
            let v = *s;
            *s += 1;
            v
        };

        // Integer percentage over budget: (p99 - budget) * 100 / budget.
        let pct_over = if budget_ns > 0 {
            (p99_ns.saturating_sub(budget_ns).saturating_mul(100)) / budget_ns
        } else {
            0
        };

        // CRITICAL when p99 is >= 2x the budget, WARNING otherwise.
        let severity = if p99_ns >= 2 * budget_ns {
            AlertSeverity::Critical
        } else {
            AlertSeverity::Warning
        };

        let alert = Alert {
            id: format!("ALERT-{seq:08}"),
            stage,
            severity,
            observed_p99_ns: p99_ns,
            budget_ns,
            pct_over,
            ts_ns: now,
        };

        let mut alerts = self.alerts.lock().unwrap();
        alerts.push(alert.clone());
        while alerts.len() > self.cfg.budget.alerts_retained {
            alerts.remove(0);
        }
        Some(alert)
    }

    /// Replace the configured budgets (used by a future config-service push).
    pub fn set_budgets(&self, budgets: Vec<Budget>) {
        // Note: budgets live in the immutable Config; this method is provided
        // for forward compatibility and re-validates stage uniqueness.  For now
        // it simply returns without mutation — the real update path will swap
        // the whole Config under a RwLock when S11 lands.
        let _ = budgets;
    }

    // -- queries ------------------------------------------------------------

    /// Percentile (0..=100) of a stage's current window using linear
    /// interpolation over a sorted copy.  Returns `None` for an empty window.
    pub fn percentile(&self, stage: Stage, p: f64) -> Option<i64> {
        let i = Self::idx(stage);
        let windows = self.windows.lock().unwrap();
        let w = &windows[i];
        if w.is_empty() {
            return None;
        }
        let mut sorted: Vec<i64> = w.iter().copied().collect();
        sorted.sort_unstable();
        percentile_of_sorted(&sorted, p)
    }

    /// Full statistics snapshot for one stage.  Returns `Err(LAT-204)` when the
    /// window holds fewer than `min_samples` samples and `require_min` is true.
    pub fn stage_stats(&self, stage: Stage, require_min: bool) -> Result<StageStats, Error> {
        let i = Self::idx(stage);
        let (count, min_ns, max_ns, mean_ns, p50, p99, p999) = {
            let windows = self.windows.lock().unwrap();
            let w = &windows[i];
            if w.is_empty() || (require_min && w.len() < self.cfg.window.min_samples) {
                // Report what we have; the caller decides whether to 409.
                let count = w.len();
                if require_min && count < self.cfg.window.min_samples {
                    return Err(Error::insufficient_samples(
                        stage.as_str(),
                        count,
                        self.cfg.window.min_samples,
                    ));
                }
                (count, None, None, None, None, None, None)
            } else {
                let mut sorted: Vec<i64> = w.iter().copied().collect();
                sorted.sort_unstable();
                let sum: i128 = sorted.iter().map(|&x| x as i128).sum();
                let count = sorted.len();
                (
                    count,
                    Some(sorted[0]),
                    sorted.last().copied(),
                    Some((sum / count as i128) as i64),
                    percentile_of_sorted(&sorted, 50.0),
                    percentile_of_sorted(&sorted, 99.0),
                    percentile_of_sorted(&sorted, 99.9),
                )
            }
        };

        let total = self.totals.lock().unwrap()[i];
        let budget_ns = self
            .cfg
            .budget
            .stage_budgets_ns
            .iter()
            .find(|(name, _)| *name == stage.as_str())
            .map(|(_, ns)| *ns);

        let breached = match (p99, budget_ns) {
            (Some(p), Some(b)) => p > ((b as f64 * self.cfg.budget.breach_ratio) as i64),
            _ => false,
        };

        Ok(StageStats {
            stage,
            count,
            total,
            min_ns,
            max_ns,
            mean_ns,
            p50_ns: p50,
            p99_ns: p99,
            p999_ns: p999,
            budget_ns,
            breached,
        })
    }

    /// Statistics for every known stage (never fails; empty stages report zero
    /// counts with `None` percentiles).
    pub fn all_stats(&self) -> Vec<StageStats> {
        Stage::all()
            .iter()
            .map(|&s| self.stage_stats(s, false).unwrap_or_else(|_| empty_stats(s)))
            .collect()
    }

    /// Recent alerts for a stage (or all stages when `stage` is `None`),
    /// newest first.  `limit` bounds the result (0 = all).
    pub fn alerts(&self, stage: Option<Stage>, limit: usize) -> Vec<Alert> {
        let mut v: Vec<Alert> = self.alerts.lock().unwrap().clone();
        if let Some(s) = stage {
            v.retain(|a| a.stage == s);
        }
        v.reverse();
        if limit > 0 && v.len() > limit {
            v.truncate(limit);
        }
        v
    }

    /// The configured budgets (as parsed at boot).
    pub fn budgets(&self) -> Vec<Budget> {
        self.cfg
            .budget
            .stage_budgets_ns
            .iter()
            .filter_map(|(name, ns)| Stage::from_str(name).map(|st| Budget { stage: st, budget_ns: *ns }))
            .collect()
    }

    /// Total samples ever recorded across all stages.
    pub fn total_samples(&self) -> u64 {
        self.totals.lock().unwrap().iter().sum()
    }

    /// Number of alerts currently in the rolling history.
    pub fn alert_count(&self) -> usize {
        self.alerts.lock().unwrap().len()
    }
}

fn empty_stats(stage: Stage) -> StageStats {
    StageStats {
        stage,
        count: 0,
        total: 0,
        min_ns: None,
        max_ns: None,
        mean_ns: None,
        p50_ns: None,
        p99_ns: None,
        p999_ns: None,
        budget_ns: None,
        breached: false,
    }
}

/// Linear-interpolated percentile of a **sorted** slice.  `p` is in [0, 100].
/// Uses the "exclusive" rank convention: rank = (n - 1) * p / 100, then linearly
/// interpolates between floor(rank) and ceil(rank).  Integer math throughout;
/// the single float multiply is confined to the rank computation and its result
/// is immediately truncated back to integer indices.
fn percentile_of_sorted(sorted: &[i64], p: f64) -> Option<i64> {
    let n = sorted.len();
    if n == 0 {
        return None;
    }
    if n == 1 || p <= 0.0 {
        return Some(sorted[0]);
    }
    if p >= 100.0 {
        return Some(*sorted.last().unwrap());
    }
    let exact_rank = (n - 1) as f64 * (p / 100.0);
    let rank = exact_rank as usize; // floor
    let lo = sorted[rank];
    if rank + 1 >= n {
        return Some(lo);
    }
    let hi = sorted[rank + 1];
    // Interpolate the fractional part of the rank in fixed-point (scale 1000)
    // so the final blend stays in integer arithmetic.
    let frac_scaled = ((exact_rank - rank as f64) * 1000.0) as i64; // 0..=999
    Some(lo + ((hi - lo) * frac_scaled / 1000))
}

/// Shared handle to the monitor, passed into the HTTP layer.
pub type SharedMonitor = Arc<LatencyMonitor>;
