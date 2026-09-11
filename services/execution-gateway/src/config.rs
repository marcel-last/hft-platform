//! execution_gateway — configuration.
//!
//! Mirrors the Python services' frozen-config pattern: a set of immutable
//! structs with sensible defaults plus a `validate` function that returns a
//! list of error strings (empty = valid).  No environment-variable reading
//! here; deployment overrides are applied in `main.rs`.

use std::fmt;

/// Venue connection + order-sizing policy.
#[derive(Debug, Clone)]
pub struct VenueConfig {
    /// Default venue id used when an intent does not specify one.
    pub default_venue: String,
    /// Simulated round-trip latency for a venue ack (ns). 0 = immediate.
    pub simulated_ack_latency_ns: u64,
    /// Probability in [0,1] that a simulated order is rejected by the venue.
    pub simulated_reject_rate: f64,
}

impl Default for VenueConfig {
    fn default() -> Self {
        VenueConfig {
            default_venue: "SIM".to_string(),
            simulated_ack_latency_ns: 0,
            simulated_reject_rate: 0.0,
        }
    }
}

/// Order-lifecycle tuning.
#[derive(Debug, Clone, Copy)]
pub struct LifecycleConfig {
    /// Max open (NEW/PARTIAL/FILLED) orders kept before the oldest is pruned.
    pub max_open_orders: usize,
    /// Rolling fill history length retained for the /fills endpoint.
    pub fills_retained: usize,
    /// Max quantity accepted on a single order intent.
    pub max_order_qty: i64,
    /// Minimum price accepted (rejects empty-book garbage).
    pub min_price: f64,
}

impl Default for LifecycleConfig {
    fn default() -> Self {
        LifecycleConfig {
            max_open_orders: 10_000,
            fills_retained: 4_096,
            max_order_qty: 10_000,
            min_price: 0.0,
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
    pub venue: VenueConfig,
    pub lifecycle: LifecycleConfig,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            name: "execution-gateway".to_string(),
            version: "1.0.0".to_string(),
            env: "production".to_string(),
            listen_port: 7640,
            venue: VenueConfig::default(),
            lifecycle: LifecycleConfig::default(),
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
        if self.venue.default_venue.is_empty() {
            errs.push("venue.default_venue must not be empty".to_string());
        }
        if !(0.0..=1.0).contains(&self.venue.simulated_reject_rate) {
            errs.push(format!(
                "venue.simulated_reject_rate must be in [0,1]: {}",
                self.venue.simulated_reject_rate
            ));
        }
        if self.lifecycle.max_open_orders < 1 {
            errs.push("lifecycle.max_open_orders must be >= 1".to_string());
        }
        if self.lifecycle.fills_retained < 1 {
            errs.push("lifecycle.fills_retained must be >= 1".to_string());
        }
        if self.lifecycle.max_order_qty < 1 {
            errs.push("lifecycle.max_order_qty must be >= 1".to_string());
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
