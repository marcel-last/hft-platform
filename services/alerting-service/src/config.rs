//! alerting_service — configuration.
//!
//! Mirrors the Python services' frozen-config pattern: a set of immutable
//! structs with sensible defaults plus a `validate` function that returns a
//! list of error strings (empty = valid).  No environment-variable reading
//! here; deployment overrides are applied in `main.rs`.

use std::fmt;

/// Deduplication / suppression tuning.
#[derive(Debug, Clone, Copy)]
pub struct DedupConfig {
    /// Two alerts from the same `(source, category)` with identical context and
    /// severity within this window (ns) are treated as one logical alert: the
    /// occurrence count is incremented and no new dispatch is emitted.  Must be
    /// >= 0.
    pub dedup_window_ns: i64,
    /// While a key's most recent alert is still open (un-acknowledged), any
    /// further occurrences are suppressed into the same logical alert for up to
    /// this window (ns).  Must be >= `dedup_window_ns`.
    pub suppression_window_ns: i64,
}

impl Default for DedupConfig {
    fn default() -> Self {
        DedupConfig {
            dedup_window_ns: 30_000_000_000, // 30 s
            suppression_window_ns: 5 * 60_000_000_000, // 5 min
        }
    }
}

/// Escalation policy.
#[derive(Debug, Clone, Copy)]
pub struct EscalationConfig {
    /// A WARNING alert that has not been acknowledged for this long (ns) is
    /// escalated to CRITICAL.  Must be > 0.
    pub warning_escalate_after_ns: i64,
    /// A CRITICAL alert that has not been acknowledged for this long (ns) is
    /// flagged `needs_oncall` (the on-call page would fire in a real deploy).
    /// Must be > 0 and >= `warning_escalate_after_ns`, so an alert escalates
    /// to CRITICAL before it can reach the on-call threshold.
    pub critical_escalate_after_ns: i64,
}

impl Default for EscalationConfig {
    fn default() -> Self {
        EscalationConfig {
            warning_escalate_after_ns: 5 * 60_000_000_000, // 5 min
            critical_escalate_after_ns: 15 * 60_000_000_000, // 15 min
        }
    }
}

/// Bounded-history tuning.
#[derive(Debug, Clone, Copy)]
pub struct HistoryConfig {
    /// Number of logical alerts retained in memory (newest kept).  Must be >= 1.
    pub retained: usize,
    /// Maximum number of occurrences recorded per logical alert before the
    /// counter saturates (prevents unbounded growth for very chatty keys).
    /// Must be >= 2.
    pub max_occurrences: u32,
}

impl Default for HistoryConfig {
    fn default() -> Self {
        HistoryConfig {
            retained: 4_096,
            max_occurrences: 10_000,
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
    /// Upstream services that are permitted to POST alerts.  An empty list
    /// means "accept any source" (open mode).
    pub allowed_sources: Vec<String>,
    pub dedup: DedupConfig,
    pub escalation: EscalationConfig,
    pub history: HistoryConfig,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            name: "alerting-service".to_string(),
            version: "1.0.0".to_string(),
            env: "production".to_string(),
            listen_port: 7700,
            // The three known alert producers.  Empty in a real deploy would be
            // open; here we pin the expected set so tests can exercise both.
            allowed_sources: vec![
                "risk-manager".to_string(),
                "latency-monitor".to_string(),
                "data-quality-monitor".to_string(),
            ],
            dedup: DedupConfig::default(),
            escalation: EscalationConfig::default(),
            history: HistoryConfig::default(),
        }
    }
}

impl Config {
    /// Validate the configuration; returns a list of error strings.
    pub fn validate(&self) -> Vec<String> {
        let mut errs = Vec::new();
        if self.listen_port == 0 {
            errs.push("listen_port must be >= 1".to_string());
        }
        if self.name.is_empty() {
            errs.push("name must not be empty".to_string());
        }
        if self.dedup.dedup_window_ns < 0 {
            errs.push("dedup.dedup_window_ns must be >= 0".to_string());
        }
        if self.dedup.suppression_window_ns < self.dedup.dedup_window_ns {
            errs.push(
                "dedup.suppression_window_ns must be >= dedup.dedup_window_ns".to_string(),
            );
        }
        if self.escalation.warning_escalate_after_ns <= 0 {
            errs.push("escalation.warning_escalate_after_ns must be > 0".to_string());
        }
        if self.escalation.critical_escalate_after_ns <= 0 {
            errs.push("escalation.critical_escalate_after_ns must be > 0".to_string());
        }
        if self.escalation.critical_escalate_after_ns < self.escalation.warning_escalate_after_ns {
            errs.push(
                "escalation.critical_escalate_after_ns must be >= warning_escalate_after_ns"
                    .to_string(),
            );
        }
        if self.history.retained < 1 {
            errs.push("history.retained must be >= 1".to_string());
        }
        if self.history.max_occurrences < 2 {
            errs.push("history.max_occurrences must be >= 2".to_string());
        }
        // Allowed sources: no duplicates.
        let mut seen = std::collections::HashSet::new();
        for src in &self.allowed_sources {
            if src.is_empty() {
                errs.push("allowed_sources entry must not be empty".to_string());
            } else if !seen.insert(src.clone()) {
                errs.push(format!("duplicate allowed source {src:?}"));
            }
        }
        errs
    }

    /// True when the given source is permitted to submit alerts.  An empty
    /// `allowed_sources` list means open mode (everything accepted).
    pub fn allows_source(&self, source: &str) -> bool {
        if self.allowed_sources.is_empty() {
            return true;
        }
        self.allowed_sources.iter().any(|s| s == source)
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
