//! latency_monitor — error taxonomy.
//!
//! Codes follow the platform convention: `LAT-<NNN>` for latency-monitor.
//! Every variant carries the standard `(code, message, retryable)` triple and
//! serializes to the identical JSON envelope used by every other service.
//! (No external crates: the envelope is built with the in-crate `json` module.)

use std::collections::HashMap;
use std::error::Error as StdError;
use std::fmt;

/// A single latency-monitor error with its wire representation.
#[derive(Debug, Clone)]
pub struct Error {
    pub code: &'static str,
    pub message: String,
    pub retryable: bool,
    /// HTTP status to return when this error is surfaced on the request path.
    pub http_status: u16,
    /// Structured debug fields, insertion-ordered via an accompanying Vec.
    pub context: HashMap<String, crate::json::Value>,
}

impl Error {
    fn new(code: &'static str, message: impl Into<String>, retryable: bool, http_status: u16) -> Self {
        Error {
            code,
            message: message.into(),
            retryable,
            http_status,
            context: HashMap::new(),
        }
    }

    /// Attach a structured debug field to the error context.
    pub fn with(mut self, key: &str, value: crate::json::Value) -> Self {
        self.context.insert(key.to_string(), value);
        self
    }

    /// Serialize this error to the standard platform envelope.
    pub fn to_envelope(&self) -> crate::json::Value {
        let mut ctx: crate::json::Map = Vec::new();
        for (k, v) in &self.context {
            ctx.push((k.clone(), v.clone()));
        }
        crate::json::Value::Object(vec![
            ("error".to_string(), crate::json::Value::Object(vec![
                ("code".to_string(), crate::json::Value::String(self.code.to_string())),
                ("message".to_string(), crate::json::Value::String(self.message.clone())),
                ("service".to_string(), crate::json::Value::String("latency-monitor".to_string())),
                ("retryable".to_string(), crate::json::Value::Bool(self.retryable)),
                ("context".to_string(), crate::json::Value::Object(ctx)),
            ]))
        ])
    }
}

// -- convenience constructors -------------------------------------------------

impl Error {
    pub fn config(message: impl Into<String>) -> Self {
        Self::new("LAT-101", message, false, 503)
    }

    /// LAT-200 — malformed request body (400).
    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::new("LAT-200", message, false, 400)
    }

    /// LAT-201 — unknown stage identifier (404).
    pub fn unknown_stage(stage: &str) -> Self {
        Self::new(
            "LAT-201",
            format!("no latency data for unknown stage {stage:?}"),
            false,
            404,
        )
        .with("stage", crate::json::Value::String(stage.to_string()))
    }

    /// LAT-202 — inverted timestamps (t1 < t0) on a stamp request (400).
    pub fn inverted_timestamps(stage: &str, t0_ns: i64, t1_ns: i64) -> Self {
        Self::new(
            "LAT-202",
            format!("inverted timestamps for stage {stage}: t1 < t0"),
            false,
            400,
        )
        .with("stage", crate::json::Value::String(stage.to_string()))
        .with("t0_ns", crate::json::Value::Int(t0_ns))
        .with("t1_ns", crate::json::Value::Int(t1_ns))
    }

    /// LAT-203 — negative explicit duration (400).
    pub fn negative_duration(stage: &str, d: i64) -> Self {
        Self::new(
            "LAT-203",
            format!("negative duration for stage {stage}: {d} ns"),
            false,
            400,
        )
        .with("stage", crate::json::Value::String(stage.to_string()))
        .with("duration_ns", crate::json::Value::Int(d))
    }

    /// LAT-204 — stage has insufficient samples to report percentiles (409).
    pub fn insufficient_samples(stage: &str, have: usize, need: usize) -> Self {
        Self::new(
            "LAT-204",
            format!(
                "stage {stage} has {have} samples; at least {need} required for percentiles"
            ),
            false,
            409,
        )
        .with("stage", crate::json::Value::String(stage.to_string()))
        .with("samples", crate::json::Value::Int(have as i64))
        .with("required", crate::json::Value::Int(need as i64))
    }

    /// LAT-301 — internal invariant violation (500).
    pub fn internal(message: impl Into<String>) -> Self {
        Self::new("LAT-900", message, false, 500)
    }

    pub fn not_found(method: &str, path: &str) -> Self {
        Self::new("LAT-404", format!("no route for {method} {path}"), false, 404)
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", self.code, self.message)
    }
}

impl StdError for Error {}

/// Serialize an arbitrary error (used when a handler returns `Err(Error)`).
pub fn envelope(err: &Error) -> crate::json::Value {
    err.to_envelope()
}
