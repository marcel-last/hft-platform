//! alerting_service — domain models and wire (de)serialization.
//!
//! Models the :struct:`AlertEvent` that upstream producers POST to `/alerts`,
//! the lifecycle :enum:`AlertState` of a logical alert, and the derived
//! :struct:`AckTicket` used to acknowledge an alert.  All timestamps are `i64`
//! nanoseconds since the Unix epoch — no floating point anywhere in the
//! alerting math.

use std::fmt;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::json::{self, Value};

/// Current time as `i64` nanoseconds since the Unix epoch (hot-path convention).
pub fn now_ns() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as i64)
        .unwrap_or(0)
}

/// Alert severity.  Ordered so that escalation can compare "more severe".
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    Info = 0,
    Warning = 1,
    Critical = 2,
}

impl Severity {
    pub fn as_str(self) -> &'static str {
        match self {
            Severity::Info => "INFO",
            Severity::Warning => "WARNING",
            Severity::Critical => "CRITICAL",
        }
    }

    /// Parse a severity from its wire name (case-insensitive).  Accepts a small
    /// set of synonyms so upstream producers can be slightly loose.
    pub fn from_str(s: &str) -> Option<Self> {
        Some(match s.to_ascii_uppercase().as_str() {
            "INFO" | "LOW" => Severity::Info,
            "WARNING" | "WARN" | "MEDIUM" => Severity::Warning,
            "CRITICAL" | "CRIT" | "HIGH" | "ERROR" => Severity::Critical,
            _ => return None,
        })
    }

    /// All severities in ascending order.
    pub fn all() -> [Severity; 3] {
        [Severity::Info, Severity::Warning, Severity::Critical]
    }
}

impl fmt::Display for Severity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

/// Lifecycle state of a logical alert.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AlertState {
    /// The alert is active and awaiting acknowledgement.
    Active,
    /// A human/operator has acknowledged it; it no longer escalates.
    Acknowledged,
    /// The condition cleared upstream (or the suppression window lapsed) and
    /// the alert was closed.
    Resolved,
}

impl AlertState {
    pub fn as_str(self) -> &'static str {
        match self {
            AlertState::Active => "ACTIVE",
            AlertState::Acknowledged => "ACKNOWLEDGED",
            AlertState::Resolved => "RESOLVED",
        }
    }

    /// True for states that are no longer awaiting action.
    pub fn is_terminal(self) -> bool {
        matches!(self, AlertState::Acknowledged | AlertState::Resolved)
    }
}

/// A single alert occurrence as POSTed by an upstream producer.
///
/// The wire shape (documented for S5/S7/S8 producers):
/// ```json
/// {
///   "source": "risk-manager",
///   "category": "kill_switch_engaged",
///   "severity": "CRITICAL",
///   "message": "Kill switch engaged on MAIN",
///   "context": {"account": "MAIN"},
///   "ts_ns": 1788940747832091143,
///   "id": "optional-client-id"
/// }
/// ```
#[derive(Debug, Clone)]
pub struct AlertEvent {
    /// Upstream service name (e.g. `"risk-manager"`).
    pub source: String,
    /// A stable category/condition identifier used for deduplication
    /// (e.g. `"kill_switch_engaged"`, `"feed_stale"`).
    pub category: String,
    pub severity: Severity,
    /// Human-readable single-line description.
    pub message: String,
    /// Free-form structured context.  Together with `source` + `category` this
    /// defines the deduplication key.
    pub context: Value,
    /// The timestamp of the occurrence (ns).  Falls back to receive time when
    /// absent from the body.
    pub ts_ns: i64,
    /// Wall-clock time this event was received by the alerting service (ns).
    pub received_ns: i64,
    /// Optional client-supplied id; otherwise the manager assigns one.
    pub id: Option<String>,
}

impl AlertEvent {
    /// Parse an incoming `POST /alerts` body.  `received_ns` is stamped by the
    /// caller (the alerting service's clock).  Missing/invalid required fields
    /// yield a `bad_request` error.
    pub fn parse(body: &Value, received_ns: i64) -> Result<Self, crate::errors::Error> {
        let source = body
            .get("source")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .ok_or_else(|| {
                crate::errors::Error::bad_request("missing or empty 'source' field")
            })?;

        let category = body
            .get("category")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .ok_or_else(|| {
                crate::errors::Error::bad_request("missing or empty 'category' field")
            })?;

        let severity = body
            .get("severity")
            .and_then(|v| v.as_str())
            .and_then(Severity::from_str)
            .ok_or_else(|| {
                crate::errors::Error::bad_request(
                    "missing or invalid 'severity' field (expected INFO/WARNING/CRITICAL)",
                )
            })?;

        let message = body
            .get("message")
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .unwrap_or_default();

        let context = match body.get("context") {
            Some(v) if v.is_object() => v.clone(),
            _ => Value::Object(Vec::new()),
        };

        let ts_ns = body
            .get("ts_ns")
            .and_then(|v| v.as_i64())
            .unwrap_or(received_ns);

        let id = body
            .get("id")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_string);

        Ok(AlertEvent {
            source,
            category,
            severity,
            message,
            context,
            ts_ns,
            received_ns,
            id,
        })
    }

    /// Serialize the raw event (what a producer sent) to the wire shape.
    pub fn to_json(&self) -> Value {
        let mut pairs = vec![
            ("source".into(), Value::String(self.source.clone())),
            ("category".into(), Value::String(self.category.clone())),
            (
                "severity".into(),
                Value::String(self.severity.as_str().to_string()),
            ),
            ("message".into(), Value::String(self.message.clone())),
            ("context".into(), self.context.clone()),
            ("ts_ns".into(), Value::Int(self.ts_ns)),
        ];
        if let Some(i) = &self.id {
            pairs.push(("id".into(), Value::String(i.clone())));
        }
        Value::Object(pairs)
    }

    /// The deduplication key: `(source, category, canonical-context-json)`.
    /// Severity is intentionally NOT part of the key so that a severity change
    /// on an open alert is treated as an escalation of the same logical alert.
    pub fn dedup_key(&self) -> String {
        format!("{}|{}|{}", self.source, self.category, self.context.to_json())
    }
}

/// A ticket returned when an alert is acknowledged, so the caller can later
/// confirm the resolution or re-query state.
#[derive(Debug, Clone)]
pub struct AckTicket {
    pub alert_id: String,
    pub state: AlertState,
    /// The severity at the moment of acknowledgement (post-escalation).
    pub severity: Severity,
    /// Whether an on-call escalation was pending at ack time.
    pub needs_oncall: bool,
    pub ts_ns: i64,
}

impl AckTicket {
    pub fn to_json(&self) -> Value {
        Value::Object(vec![
            ("alert_id".into(), Value::String(self.alert_id.clone())),
            (
                "state".into(),
                Value::String(self.state.as_str().to_string()),
            ),
            (
                "severity".into(),
                Value::String(self.severity.as_str().to_string()),
            ),
            ("needs_oncall".into(), Value::Bool(self.needs_oncall)),
            ("ts_ns".into(), Value::Int(self.ts_ns)),
        ])
    }
}

/// Convenience: parse a JSON string into a `Value` or map the error.
pub fn parse_json(s: &str) -> Result<Value, crate::errors::Error> {
    json::parse(s).map_err(|e| crate::errors::Error::bad_request(format!("invalid JSON: {e}")))
}
