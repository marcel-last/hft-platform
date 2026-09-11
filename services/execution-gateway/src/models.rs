//! execution_gateway — domain models and wire (de)serialization.
//!
//! Models the order lifecycle: an :enum:`OrderState` machine driven by venue
//! acknowledgments/fills, plus the :struct:`Fill` records reported downstream.
//! All timestamps are `i64` nanoseconds since the Unix epoch.

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

/// Current lifecycle state of an order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderState {
    New,
    PartiallyFilled,
    Filled,
    Canceled,
    Rejected,
}

impl OrderState {
    pub fn as_str(self) -> &'static str {
        match self {
            OrderState::New => "NEW",
            OrderState::PartiallyFilled => "PARTIALLY_FILLED",
            OrderState::Filled => "FILLED",
            OrderState::Canceled => "CANCELED",
            OrderState::Rejected => "REJECTED",
        }
    }

    /// True if the order is still live (can be modified/canceled).
    pub fn is_open(self) -> bool {
        matches!(self, OrderState::New | OrderState::PartiallyFilled)
    }

    /// Whether a transition from `self` to `next` is legal.
    pub fn can_transition_to(self, next: Self) -> bool {
        use OrderState::*;
        match (self, next) {
            // an open order may fill (fully or partially) or be canceled/rejected
            (New | PartiallyFilled, Filled | Canceled | Rejected | PartiallyFilled) => true,
            // a new order can stay new (idempotent re-ack)
            (New, New) => true,
            // terminal states are stable except rejection of an already-open order
            _ => false,
        }
    }
}

impl fmt::Display for OrderState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

/// Side of the order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderSide {
    Buy,
    Sell,
}

impl OrderSide {
    pub fn as_str(self) -> &'static str {
        match self {
            OrderSide::Buy => "BUY",
            OrderSide::Sell => "SELL",
        }
    }

    pub fn from_str(s: &str) -> Option<Self> {
        match s.to_ascii_uppercase().as_str() {
            "BUY" | "BID" => Some(OrderSide::Buy),
            "SELL" | "ASK" => Some(OrderSide::Sell),
            _ => None,
        }
    }
}

/// A single order tracked by the gateway.
#[derive(Debug, Clone)]
pub struct Order {
    pub id: String,
    /// Upstream strategy-engine signal that produced this intent (if any).
    pub signal_id: Option<String>,
    pub strategy_id: Option<String>,
    pub symbol: String,
    pub venue: String,
    pub side: OrderSide,
    pub qty: i64,
    pub filled_qty: i64,
    pub limit_price: f64,
    pub state: OrderState,
    pub created_ns: i64,
    pub updated_ns: i64,
}

impl Order {
    /// Remaining (unfilled) quantity.
    pub fn remaining_qty(&self) -> i64 {
        self.qty - self.filled_qty
    }

    /// Serialize to the wire shape used by /orders and /orders/{id}.
    pub fn to_json(&self) -> Value {
        let mut pairs = vec![
            ("id".into(), Value::String(self.id.clone())),
            ("symbol".into(), Value::String(self.symbol.clone())),
            ("venue".into(), Value::String(self.venue.clone())),
            ("side".into(), Value::String(self.side.as_str().to_string())),
            ("qty".into(), Value::Int(self.qty)),
            ("filled_qty".into(), Value::Int(self.filled_qty)),
            ("remaining_qty".into(), Value::Int(self.remaining_qty())),
            ("limit_px".into(), Value::Float(self.limit_price)),
            ("state".into(), Value::String(self.state.as_str().to_string())),
            ("created_ns".into(), Value::Int(self.created_ns)),
            ("updated_ns".into(), Value::Int(self.updated_ns)),
        ];
        if let Some(sig) = &self.signal_id {
            pairs.push(("signal_id".into(), Value::String(sig.clone())));
        }
        if let Some(strat) = &self.strategy_id {
            pairs.push(("strategy_id".into(), Value::String(strat.clone())));
        }
        Value::Object(pairs)
    }
}

/// A fill (execution report) for one order.
#[derive(Debug, Clone)]
pub struct Fill {
    pub id: String,
    pub order_id: String,
    pub symbol: String,
    pub venue: String,
    pub side: OrderSide,
    pub qty: i64,
    pub price: f64,
    pub ts_ns: i64,
}

impl Fill {
    pub fn to_json(&self) -> Value {
        Value::Object(vec![
            ("id".into(), Value::String(self.id.clone())),
            ("order_id".into(), Value::String(self.order_id.clone())),
            ("symbol".into(), Value::String(self.symbol.clone())),
            ("venue".into(), Value::String(self.venue.clone())),
            ("side".into(), Value::String(self.side.as_str().to_string())),
            ("qty".into(), Value::Int(self.qty)),
            ("price".into(), Value::Float(self.price)),
            ("ts_ns".into(), Value::Int(self.ts_ns)),
        ])
    }
}

// ---------------------------------------------------------------------------
// Wire parsing (S3 order-intent -> Order)
// ---------------------------------------------------------------------------

/// Parse an incoming order intent (from the strategy engine, S3) into an
/// :struct:`Order`.  Accepts both the S3 `to_dict` keys and a few aliases.
pub fn parse_intent(body: &Value, default_venue: &str, now_ns: i64) -> Result<Order, crate::errors::Error> {
    let symbol = body
        .get("symbol")
        .or_else(|| body.get("sym"))
        .and_then(|v| v.as_str())
        .ok_or_else(|| crate::errors::Error::bad_request("missing 'symbol' in order intent"))?
        .to_string();

    let side = body
        .get("side")
        .and_then(|v| v.as_str())
        .and_then(OrderSide::from_str)
        .ok_or_else(|| crate::errors::Error::bad_request("missing or invalid 'side' (BUY/SELL)"))?;

    let qty = body
        .get("qty")
        .or_else(|| body.get("quantity"))
        .and_then(|v| v.as_i64())
        .ok_or_else(|| crate::errors::Error::bad_request("missing or invalid 'qty'"))?;

    let limit_price = body
        .get("limit_px")
        .or_else(|| body.get("limit_price"))
        .or_else(|| body.get("px"))
        .and_then(|v| v.as_f64())
        .ok_or_else(|| crate::errors::Error::bad_request("missing or invalid 'limit_px'"))?;

    let venue = body
        .get("venue")
        .or_else(|| body.get("ven"))
        .and_then(|v| v.as_str())
        .unwrap_or(default_venue)
        .to_string();

    let id = body
        .get("id")
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .unwrap_or_else(|| format!("ORD-{}", now_ns));

    let signal_id = body.get("signal_id").and_then(|v| v.as_str()).map(str::to_string);
    let strategy_id = body.get("strategy_id").and_then(|v| v.as_str()).map(str::to_string);

    Ok(Order {
        id,
        signal_id,
        strategy_id,
        symbol,
        venue,
        side,
        qty,
        filled_qty: 0,
        limit_price,
        state: OrderState::New,
        created_ns: now_ns,
        updated_ns: now_ns,
    })
}

/// Parse a modify request body (PATCH /orders/{id}) into the fields to change.
pub struct ModifyRequest {
    pub qty: Option<i64>,
    pub limit_price: Option<f64>,
}

impl ModifyRequest {
    pub fn parse(body: &Value) -> Result<Self, crate::errors::Error> {
        let qty = body.get("qty").or_else(|| body.get("quantity")).and_then(|v| v.as_i64());
        let limit_price = body
            .get("limit_px")
            .or_else(|| body.get("limit_price"))
            .and_then(|v| v.as_f64());
        if qty.is_none() && limit_price.is_none() {
            return Err(crate::errors::Error::bad_request(
                "modify body must include 'qty' and/or 'limit_px'",
            ));
        }
        Ok(ModifyRequest { qty, limit_price })
    }
}

/// Convenience: parse a JSON string into a `Value` or map the error.
pub fn parse_json(s: &str) -> Result<Value, crate::errors::Error> {
    json::parse(s).map_err(|e| crate::errors::Error::bad_request(format!("invalid JSON: {e}")))
}
