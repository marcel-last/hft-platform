//! execution_gateway — error taxonomy.
//!
//! Codes follow the platform convention: `EXG-<NNN>` for execution-gateway.
//! Every variant carries the standard `(code, message, retryable)` triple and
//! serializes to the identical JSON envelope used by every other service.
//! (No external crates: the envelope is built with the in-crate `json` module.)

use std::collections::HashMap;
use std::error::Error as StdError;
use std::fmt;

/// A single execution-gateway error with its wire representation.
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
                ("service".to_string(), crate::json::Value::String("execution-gateway".to_string())),
                ("retryable".to_string(), crate::json::Value::Bool(self.retryable)),
                ("context".to_string(), crate::json::Value::Object(ctx)),
            ]))
        ])
    }
}

// -- convenience constructors -------------------------------------------------

impl Error {
    pub fn config(message: impl Into<String>) -> Self {
        Self::new("EXG-101", message, false, 503)
    }

    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::new("EXG-200", message, false, 400)
    }

    pub fn unknown_order(order_id: &str) -> Self {
        Self::new("EXG-201", format!("no order with id {order_id:?}"), false, 404)
            .with("order_id", crate::json::Value::String(order_id.to_string()))
    }

    pub fn invalid_state_transition(order_id: &str, from: &str, to: &str) -> Self {
        Self::new(
            "EXG-202",
            format!("order {order_id} cannot transition {from} -> {to}"),
            false,
            409,
        )
        .with("order_id", crate::json::Value::String(order_id.to_string()))
        .with("from", crate::json::Value::String(from.to_string()))
        .with("to", crate::json::Value::String(to.to_string()))
    }

    pub fn venue_unreachable(venue: &str) -> Self {
        Self::new("EXG-301", format!("venue {venue} unreachable"), true, 502)
            .with("venue", crate::json::Value::String(venue.to_string()))
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self::new("EXG-900", message, false, 500)
    }

    pub fn not_found(method: &str, path: &str) -> Self {
        Self::new("EXG-404", format!("no route for {method} {path}"), false, 404)
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
