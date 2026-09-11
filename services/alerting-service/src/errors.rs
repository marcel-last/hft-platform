//! alerting_service — error taxonomy.
//!
//! Codes follow the platform convention: `ALT-<NNN>` for alerting-service.
//! Every variant carries the standard `(code, message, retryable)` triple and
//! serializes to the identical JSON envelope used by every other service.
//! (No external crates: the envelope is built with the in-crate `json` module.)

use std::collections::HashMap;
use std::error::Error as StdError;
use std::fmt;

/// A single alerting-service error with its wire representation.
#[derive(Debug, Clone)]
pub struct Error {
    pub code: &'static str,
    pub message: String,
    pub retryable: bool,
    /// HTTP status to return when this error is surfaced on the request path.
    pub http_status: u16,
    /// Structured debug fields (insertion order preserved at serialization).
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
                ("service".to_string(), crate::json::Value::String("alerting-service".to_string())),
                ("retryable".to_string(), crate::json::Value::Bool(self.retryable)),
                ("context".to_string(), crate::json::Value::Object(ctx)),
            ]))
        ])
    }
}

// -- convenience constructors -------------------------------------------------

impl Error {
    /// ALT-101 — configuration failed validation (503).
    pub fn config(message: impl Into<String>) -> Self {
        Self::new("ALT-101", message, false, 503)
    }

    /// ALT-200 — malformed request body / missing required field (400).
    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::new("ALT-200", message, false, 400)
    }

    /// ALT-201 — the submitting source is not in the allowed set (403).
    pub fn unauthorized_source(source: &str) -> Self {
        Self::new(
            "ALT-201",
            format!("source {source:?} is not permitted to submit alerts"),
            false,
            403,
        )
        .with("source", crate::json::Value::String(source.to_string()))
    }

    /// ALT-202 — unknown alert id (404).
    pub fn unknown_alert(alert_id: &str) -> Self {
        Self::new(
            "ALT-202",
            format!("no alert with id {alert_id:?}"),
            false,
            404,
        )
        .with("alert_id", crate::json::Value::String(alert_id.to_string()))
    }

    /// ALT-203 — attempting to acknowledge an already-resolved alert (409).
    pub fn already_resolved(alert_id: &str) -> Self {
        Self::new(
            "ALT-203",
            format!("alert {alert_id:?} is already resolved and cannot be re-acknowledged"),
            false,
            409,
        )
        .with("alert_id", crate::json::Value::String(alert_id.to_string()))
    }

    /// ALT-301 — internal invariant violation (500).
    pub fn internal(message: impl Into<String>) -> Self {
        Self::new("ALT-900", message, false, 500)
    }

    /// ALT-404 — no route for the method+path (404).
    pub fn not_found(method: &str, path: &str) -> Self {
        Self::new("ALT-404", format!("no route for {method} {path}"), false, 404)
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
