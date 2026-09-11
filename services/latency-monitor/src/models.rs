//! latency_monitor — domain models and wire (de)serialization.
//!
//! Models the five pipeline stages we instrument, a single :struct:`LatencySample`
//! observation, per-stage :struct:`Budget` policy, and the :struct:`Alert`
//! records emitted when a budget is breached.  All timestamps are `i64`
//! nanoseconds since the Unix epoch; all durations are `i64` nanoseconds — no
//! floating point anywhere in the latency math (floats appear only at the
//! serialization boundary for the p50/p99/p999 display values).

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

/// The five pipeline stages we instrument.  Each stage maps to a pair of wire
/// timestamps (see `LatencySample::parse`) and to a default budget in
/// `config::BudgetConfig`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Stage {
    VenueToGateway,
    GatewayToBook,
    BookToSignal,
    SignalToOrder,
    OrderToAck,
}

impl Stage {
    /// All known stages in pipeline order.
    pub fn all() -> [Stage; 5] {
        [
            Stage::VenueToGateway,
            Stage::GatewayToBook,
            Stage::BookToSignal,
            Stage::SignalToOrder,
            Stage::OrderToAck,
        ]
    }

    /// Canonical wire name (kebab-case) used in endpoints and budgets.
    pub fn as_str(self) -> &'static str {
        match self {
            Stage::VenueToGateway => "venue_to_gateway",
            Stage::GatewayToBook => "gateway_to_book",
            Stage::BookToSignal => "book_to_signal",
            Stage::SignalToOrder => "signal_to_order",
            Stage::OrderToAck => "order_to_ack",
        }
    }

    /// Parse a stage from its wire name (case-insensitive).
    pub fn from_str(s: &str) -> Option<Self> {
        let s = s.to_ascii_lowercase();
        Some(match s.as_str() {
            "venue_to_gateway" | "v2g" => Stage::VenueToGateway,
            "gateway_to_book" | "g2b" => Stage::GatewayToBook,
            "book_to_signal" | "b2s" => Stage::BookToSignal,
            "signal_to_order" | "s2o" => Stage::SignalToOrder,
            "order_to_ack" | "o2a" => Stage::OrderToAck,
            _ => return None,
        })
    }

    /// The two wire fields (source, dest) whose difference defines this stage's
    /// latency.  `None` if the pair is not a valid combination.
    pub fn timestamp_pair(self) -> Option<(&'static str, &'static str)> {
        match self {
            Stage::VenueToGateway => Some(("vt", "rt")),
            Stage::GatewayToBook => Some(("rt", "bt")),
            Stage::BookToSignal => Some(("bt", "st")),
            Stage::SignalToOrder => Some(("st", "ot")),
            Stage::OrderToAck => Some(("ot", "at")),
        }
    }
}

impl fmt::Display for Stage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

/// A single latency observation.  `duration_ns` is the measured stage duration
/// in nanoseconds (always non-negative; negative inputs are rejected at parse).
#[derive(Debug, Clone)]
pub struct LatencySample {
    pub id: String,
    pub stage: Stage,
    /// Optional correlation context (symbol, venue, order_id) — free-form.
    pub symbol: Option<String>,
    pub venue: Option<String>,
    pub order_id: Option<String>,
    /// The two raw timestamps that produced `duration_ns` (kept for auditing).
    pub t0_ns: i64,
    pub t1_ns: i64,
    /// Measured duration = `t1_ns - t0_ns`, in nanoseconds.
    pub duration_ns: i64,
    /// Wall-clock time this sample was recorded (ns).
    pub ts_ns: i64,
}

impl LatencySample {
    /// Build a sample from an explicit pair of timestamps.  Returns `Err` if the
    /// pair is inverted (t1 < t0) — negative latency is never legal.
    pub fn from_pair(
        id: String,
        stage: Stage,
        symbol: Option<String>,
        venue: Option<String>,
        order_id: Option<String>,
        t0_ns: i64,
        t1_ns: i64,
        ts_ns: i64,
    ) -> Result<Self, crate::errors::Error> {
        if t1_ns < t0_ns {
            return Err(crate::errors::Error::inverted_timestamps(
                stage.as_str(),
                t0_ns,
                t1_ns,
            ));
        }
        Ok(LatencySample {
            id,
            stage,
            symbol,
            venue,
            order_id,
            t0_ns,
            t1_ns,
            duration_ns: t1_ns - t0_ns,
            ts_ns,
        })
    }

    /// Parse an incoming `POST /stamp` body.  Accepts either:
    ///   * a direct `{stage, duration_ns, ...}` payload (pre-computed), or
    ///   * a raw timestamp pair `{stage, <src>, <dst>, ...}` where the two
    ///     fields are looked up per-stage via `Stage::timestamp_pair()`.
    pub fn parse(body: &Value, ts_ns: i64) -> Result<Self, crate::errors::Error> {
        let stage = body
            .get("stage")
            .and_then(|v| v.as_str())
            .and_then(Stage::from_str)
            .ok_or_else(|| {
                crate::errors::Error::bad_request("missing or invalid 'stage' field")
            })?;

        let symbol = body
            .get("symbol")
            .or_else(|| body.get("sym"))
            .and_then(|v| v.as_str())
            .map(str::to_string);
        let venue = body
            .get("venue")
            .or_else(|| body.get("ven"))
            .and_then(|v| v.as_str())
            .map(str::to_string);
        let order_id = body
            .get("order_id")
            .or_else(|| body.get("ord"))
            .and_then(|v| v.as_str())
            .map(str::to_string);

        // Prefer an explicit duration; otherwise derive from the stage's pair.
        let (t0_ns, t1_ns) = if let Some(d) = body.get("duration_ns").and_then(|v| v.as_i64()) {
            if d < 0 {
                return Err(crate::errors::Error::negative_duration(stage.as_str(), d));
            }
            // We don't have the raw pair; use ts_ns as both anchors (auditing
            // will show t0 == t1 == ts, which is fine for a pre-computed stamp).
            (ts_ns - d, ts_ns)
        } else if let Some((src, dst)) = stage.timestamp_pair() {
            let t0 = body
                .get(src)
                .and_then(|v| v.as_i64())
                .ok_or_else(|| {
                    crate::errors::Error::bad_request(format!(
                        "missing timestamp field {src:?} for stage {}",
                        stage.as_str()
                    ))
                })?;
            let t1 = body
                .get(dst)
                .and_then(|v| v.as_i64())
                .ok_or_else(|| {
                    crate::errors::Error::bad_request(format!(
                        "missing timestamp field {dst:?} for stage {}",
                        stage.as_str()
                    ))
                })?;
            (t0, t1)
        } else {
            return Err(crate::errors::Error::bad_request(
                "body must include 'duration_ns' or a stage timestamp pair",
            ));
        };

        let id = body
            .get("id")
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .unwrap_or_else(|| format!("STAMP-{}-{ts_ns}", stage.as_str()));

        Self::from_pair(id, stage, symbol, venue, order_id, t0_ns, t1_ns, ts_ns)
    }

    /// Serialize to the wire shape used by /latency and /latency/{stage}.
    pub fn to_json(&self) -> Value {
        let mut pairs = vec![
            ("id".into(), Value::String(self.id.clone())),
            ("stage".into(), Value::String(self.stage.as_str().to_string())),
            ("t0_ns".into(), Value::Int(self.t0_ns)),
            ("t1_ns".into(), Value::Int(self.t1_ns)),
            ("duration_ns".into(), Value::Int(self.duration_ns)),
            ("ts_ns".into(), Value::Int(self.ts_ns)),
        ];
        if let Some(s) = &self.symbol {
            pairs.push(("symbol".into(), Value::String(s.clone())));
        }
        if let Some(v) = &self.venue {
            pairs.push(("venue".into(), Value::String(v.clone())));
        }
        if let Some(o) = &self.order_id {
            pairs.push(("order_id".into(), Value::String(o.clone())));
        }
        Value::Object(pairs)
    }
}

/// A per-stage latency budget (ns).  An empty `budgets` list in the config
/// means "no budgets" — samples are recorded but no alerts fire.
#[derive(Debug, Clone)]
pub struct Budget {
    pub stage: Stage,
    /// The hard budget in nanoseconds.
    pub budget_ns: i64,
}

impl Budget {
    /// Serialize for /budgets.
    pub fn to_json(&self) -> Value {
        Value::Object(vec![
            ("stage".into(), Value::String(self.stage.as_str().to_string())),
            ("budget_ns".into(), Value::Int(self.budget_ns)),
        ])
    }
}

/// Severity of a budget-breach alert.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AlertSeverity {
    Warning,
    Critical,
}

impl AlertSeverity {
    pub fn as_str(self) -> &'static str {
        match self {
            AlertSeverity::Warning => "WARNING",
            AlertSeverity::Critical => "CRITICAL",
        }
    }
}

/// An alert record emitted when a stage's p99 exceeds its budget.
#[derive(Debug, Clone)]
pub struct Alert {
    pub id: String,
    pub stage: Stage,
    pub severity: AlertSeverity,
    /// The observed p99 (ns) that triggered the alert.
    pub observed_p99_ns: i64,
    /// The configured budget (ns).
    pub budget_ns: i64,
    /// How far over budget the p99 is, as a percentage of the budget (i64).
    /// E.g. `pct_over = 25` means p99 is 125% of the budget.
    pub pct_over: i64,
    pub ts_ns: i64,
}

impl Alert {
    pub fn to_json(&self) -> Value {
        Value::Object(vec![
            ("id".into(), Value::String(self.id.clone())),
            ("stage".into(), Value::String(self.stage.as_str().to_string())),
            (
                "severity".into(),
                Value::String(self.severity.as_str().to_string()),
            ),
            ("observed_p99_ns".into(), Value::Int(self.observed_p99_ns)),
            ("budget_ns".into(), Value::Int(self.budget_ns)),
            ("pct_over".into(), Value::Int(self.pct_over)),
            ("ts_ns".into(), Value::Int(self.ts_ns)),
        ])
    }
}

/// Convenience: parse a JSON string into a `Value` or map the error.
pub fn parse_json(s: &str) -> Result<Value, crate::errors::Error> {
    json::parse(s).map_err(|e| crate::errors::Error::bad_request(format!("invalid JSON: {e}")))
}
