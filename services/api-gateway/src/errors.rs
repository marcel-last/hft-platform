//! The `API-` error type for the gateway.
//!
//! Every variant carries the CONVENTIONS §1.2 `(code, message, retryable)`
//! triple plus the HTTP status and a structured `context`. `Response` objects
//! are built by `core`/`http` directly from these accessors; the gateway never
//! panics on the request path (CONVENTIONS §1.4).
//!
//! Code layout:
//!   * `API-001` protocol/boot (server-level; set as the `ServerConfig`
//!     protocol error code — bad request line, oversized body, chunked encoding)
//!   * `API-1xx` configuration / boot-time problems
//!   * `API-2xx` request-scoped rejections (auth, authorization, routing)
//!   * `API-5xx` upstream failures (retryable)
//!   * `API-999` fallback (unrouted handler)

use std::error::Error as StdError;
use std::fmt;

use crate::config::SERVICE_NAME;
use crate::json::Value;

/// The full set of conditions the gateway can surface to a caller.
#[derive(Debug, Clone)]
pub enum ApiError {
    /// Configuration or boot-time problem (500, retryable — operator may fix).
    Boot(String),
    /// Protocol-level failure handled by the server template (bad request line,
    /// oversized body, chunked encoding). Code fixed at boot.
    Protocol,
    /// No `Authorization` header or a malformed bearer value (401, retryable
    /// false).
    MissingToken,
    /// S12 rejected the token (invalid signature/expiry/revoked/unknown key).
    /// `detail` carries the S12 `context.reason` or code when available.
    TokenRejected { detail: String },
    /// Could not reach S12 or it returned a non-2xx protocol error (503,
    /// retryable).
    AuthUpstream { detail: String },
    /// The authenticated caller lacks a scope required by the route (403).
    Forbidden { required: &'static str },
    /// The path prefix is not owned by any upstream (404).
    NoRoute { method: String, path: String },
    /// The path prefix is owned but the HTTP method is not allowed on it (405).
    MethodNotAllowed { method: String, path: String, allowed: Vec<&'static str> },
    /// The upstream service could not be reached or errored (502/503, retryable).
    Upstream {
        service: &'static str,
        detail: String,
        retryable: bool,
    },
    /// The inbound body is not valid JSON (400, not retryable) — raised before
    /// a proxy POST would otherwise forward a malformed payload.
    BadBody,
    /// Fallback for an unrouted handler name (500).
    Unrouted,
}

impl ApiError {
    /// The `API-NNN` code string.
    pub fn code(&self) -> &'static str {
        match self {
            ApiError::Boot(_) => "API-100",
            ApiError::Protocol => "API-001",
            ApiError::MissingToken => "API-201",
            ApiError::TokenRejected { .. } => "API-202",
            ApiError::AuthUpstream { .. } => "API-503",
            ApiError::Forbidden { .. } => "API-203",
            ApiError::NoRoute { .. } => "API-404",
            ApiError::MethodNotAllowed { .. } => "API-405",
            ApiError::Upstream { .. } => "API-502",
            ApiError::BadBody => "API-204",
            ApiError::Unrouted => "API-999",
        }
    }

    /// HTTP status line for this condition.
    pub fn status(&self) -> u16 {
        match self {
            ApiError::Boot(_) => 500,
            ApiError::Protocol => 500,
            ApiError::MissingToken => 401,
            ApiError::TokenRejected { .. } => 401,
            ApiError::AuthUpstream { .. } => 503,
            ApiError::Forbidden { .. } => 403,
            ApiError::NoRoute { .. } => 404,
            ApiError::MethodNotAllowed { .. } => 405,
            ApiError::BadBody => 400,
            ApiError::Upstream { retryable, .. } => {
                // A dead upstream is retryable → 503 (service unavailable);
                // an upstream that answered with its own error → 502 (bad gateway).
                if *retryable { 503 } else { 502 }
            }
            ApiError::Unrouted => 500,
        }
    }

    /// Human-readable single-sentence message.
    pub fn message(&self) -> &'static str {
        match self {
            ApiError::Boot(_) => "Gateway is not configured to serve this request.",
            ApiError::Protocol => "Malformed or oversized HTTP request.",
            ApiError::MissingToken => "A valid Bearer token is required for this route.",
            ApiError::TokenRejected { .. } => "The presented token was rejected by auth-service.",
            ApiError::AuthUpstream { .. } => "Token verification is temporarily unavailable.",
            ApiError::Forbidden { .. } => "The authenticated subject lacks the required scope.",
            ApiError::NoRoute { .. } => "No upstream route matches this request.",
            ApiError::MethodNotAllowed { .. } => "The HTTP method is not allowed on this route.",
            ApiError::BadBody => "The request body is not valid JSON.",
            ApiError::Upstream { .. } => "The upstream service returned an error or is unreachable.",
            ApiError::Unrouted => "Internal routing mismatch.",
        }
    }

    /// Whether the caller should retry with backoff.
    pub fn retryable(&self) -> bool {
        matches!(
            self,
            ApiError::Boot(_) | ApiError::AuthUpstream { .. } | ApiError::Upstream { retryable: true, .. }
        )
    }

    /// The structured `context` object for the §1.2 envelope.
    pub fn context(&self) -> Value {
        use ApiError::*;
        match self {
            Boot(_) => Value::object(vec![("service", SERVICE_NAME.into())]),
            Protocol => Value::object(vec![]),
            MissingToken => Value::object(vec![("service", SERVICE_NAME.into())]),
            TokenRejected { detail } => Value::object(vec![(
                "detail",
                if detail.is_empty() { "rejected".into() } else { detail.clone().into() },
            )]),
            AuthUpstream { detail } => Value::object(vec![(
                "detail",
                if detail.is_empty() { "unreachable".into() } else { detail.clone().into() },
            )]),
            Forbidden { required } => Value::object(vec![
                ("service", SERVICE_NAME.into()),
                ("required_scope", (*required).into()),
            ]),
            NoRoute { method, path } => Value::object(vec![
                ("method", method.clone().into()),
                ("path", path.clone().into()),
            ]),
            MethodNotAllowed { method, path, allowed } => Value::object(vec![
                ("method", method.clone().into()),
                ("path", path.clone().into()),
                ("allowed", Value::Array(allowed.iter().map(|m| Value::String((*m).to_string())).collect())),
            ]),
            BadBody => Value::object(vec![("service", SERVICE_NAME.into())]),
            Upstream { service, detail, .. } => Value::object(vec![
                ("service", service.to_string().into()),
                (
                    "detail",
                    if detail.is_empty() { "unreachable".into() } else { detail.clone().into() },
                ),
            ]),
            Unrouted => Value::object(vec![]),
        }
    }

    /// Convenience: the fully-populated §1.2 envelope object.
    pub fn envelope(&self) -> Value {
        Value::object(vec![(
            "error",
            Value::object(vec![
                ("code", self.code().into()),
                ("message", self.message().into()),
                ("service", SERVICE_NAME.into()),
                ("retryable", self.retryable().into()),
                ("context", self.context()),
            ]),
        )])
    }
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {} (retryable={})", self.code(), self.message(), self.retryable())
    }
}

impl StdError for ApiError {
    // No underlying cause — the gateway's own diagnostics live in `context()`.
}

/// Wrap a free-form upstream failure.
impl ApiError {
    /// Build an upstream error with a stable detail string.
    pub fn upstream(service: &'static str, detail: impl Into<String>, retryable: bool) -> Self {
        ApiError::Upstream { service, detail: detail.into(), retryable }
    }
}
