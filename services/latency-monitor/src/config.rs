//! latency_monitor — configuration.
//!
//! Mirrors the Python services' frozen-config pattern: a set of immutable
//! structs with sensible defaults plus a `validate` function that returns a
//! list of error strings (empty = valid).  No environment-variable reading
//! here; deployment overrides are applied in `main.rs`.

use std::fmt;

/// Rolling-window tuning for the latency statistics engine.
#[derive(Debug, Clone, Copy)]
pub struct WindowConfig {
    /// Number of samples retained per stage before the oldest is evicted.
    pub window_size: usize,
    /// Minimum number of samples in a stage's window before percentiles are
    /// considered meaningful (stages below this report `insufficient_samples`).
    pub min_samples: usize,
}

impl Default for WindowConfig {
    fn default() -> Self {
        WindowConfig {
            window_size: 10_000,
            min_samples: 16,
        }
    }
}

/// Latency budget policy: the per-stage budgets (ns) and the alerting knobs.
#[derive(Debug, Clone)]
pub struct BudgetConfig {
    /// Per-stage latency budgets in nanoseconds.  An empty list means "no
    /// budgets configured" — samples are still recorded, but no breach alerts
    /// fire.
    pub stage_budgets_ns: Vec<(String, i64)>,
    /// A budget is considered breached when the stage's p99 over its rolling
    /// window exceeds `budget * breach_ratio`.  Must be >= 1.0.
    pub breach_ratio: f64,
    /// Cooldown between consecutive breach alerts for the same stage (ns).
    /// Prevents alert storms while a budget stays violated.
    pub alert_cooldown_ns: i64,
    /// Rolling length of the alert history retained in memory.
    pub alerts_retained: usize,
}

impl Default for BudgetConfig {
    fn default() -> Self {
        // Reasonable production budgets for the five pipeline stages (ns).
        let stage_budgets_ns = vec![
            ("venue_to_gateway".to_string(), 50_000),       // 50 µs
            ("gateway_to_book".to_string(), 20_000),        // 20 µs
            ("book_to_signal".to_string(), 100_000),        // 100 µs
            ("signal_to_order".to_string(), 500_000),       // 500 µs
            ("order_to_ack".to_string(), 1_000_000),        // 1 ms
        ];
        BudgetConfig {
            stage_budgets_ns,
            breach_ratio: 1.25,
            alert_cooldown_ns: 5_000_000_000, // 5 s
            alerts_retained: 4_096,
        }
    }
}

/// Full service configuration.
#[derive(Debug, Clone)]
pub struct Config {
    pub name: String,
    pub version: String,
    pub env: String,
    pub listen_port: u16,
    pub window: WindowConfig,
    pub budget: BudgetConfig,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            name: "latency-monitor".to_string(),
            version: "1.0.0".to_string(),
            env: "production".to_string(),
            listen_port: 7670,
            window: WindowConfig::default(),
            budget: BudgetConfig::default(),
        }
    }
}

impl Config {
    /// Validate the configuration; returns a list of error strings.
    pub fn validate(&self) -> Vec<String> {
        let mut errs = Vec::new();
        // u16 is always <= 65535, so only the zero case is invalid here.
        if self.listen_port == 0 {
            errs.push("listen_port must be >= 1".to_string());
        }
        if self.name.is_empty() {
            errs.push("name must not be empty".to_string());
        }
        if self.window.window_size < 16 {
            errs.push(format!(
                "window.window_size must be >= 16 (got {})",
                self.window.window_size
            ));
        }
        if self.window.min_samples < 2 {
            errs.push(format!(
                "window.min_samples must be >= 2 (got {})",
                self.window.min_samples
            ));
        }
        if self.window.min_samples > self.window.window_size {
            errs.push(
                "window.min_samples must be <= window.window_size".to_string(),
            );
        }
        if !(1.0..=1e6).contains(&self.budget.breach_ratio) {
            errs.push(format!(
                "budget.breach_ratio must be in [1.0, 1e6] (got {})",
                self.budget.breach_ratio
            ));
        }
        if self.budget.alert_cooldown_ns < 0 {
            errs.push(
                "budget.alert_cooldown_ns must be >= 0".to_string(),
            );
        }
        if self.budget.alerts_retained < 1 {
            errs.push("budget.alerts_retained must be >= 1".to_string());
        }
        // Stage budgets: unique, positive.
        let mut seen = std::collections::HashSet::new();
        for (stage, ns) in &self.budget.stage_budgets_ns {
            if stage.is_empty() {
                errs.push("budget.stage name must not be empty".to_string());
                continue;
            }
            if !seen.insert(stage.clone()) {
                errs.push(format!("duplicate budget for stage {stage:?}"));
            }
            if *ns <= 0 {
                errs.push(format!(
                    "budget for stage {stage:?} must be > 0 ns (got {ns})"
                ));
            }
        }
        errs
    }
}

/// A configuration that failed validation.
#[derive(Debug)]
pub struct ConfigError(pub Vec<String>);

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "configuration invalid: {}", self.0.join("; "))
    }
}
