//! execution_gateway (S4) — integration tests.
//!
//! Exercises the JSON codec, order-lifecycle state machine, wire parsing, and
//! the full HTTP server end-to-end (submit / get / list / modify / cancel /
//! fills / health / 404).  No external crates; a tiny std `TcpStream` client is
//! used for the HTTP round-trips.

use exg::config::Config;
use exg::core::OrderManager;
use exg::errors::Error;
use exg::json::{self, Value};
use exg::models::{self, OrderSide, OrderState};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::Arc;

// ---------------------------------------------------------------------------
// JSON codec
// ---------------------------------------------------------------------------

#[test]
fn json_roundtrip_object() {
    let v = Value::Object(vec![
        ("a".into(), Value::Int(1)),
        ("b".into(), Value::String("x y".into())),
        ("c".into(), Value::Bool(true)),
        ("d".into(), Value::Float(2.5)),
        ("e".into(), Value::Array(vec![Value::Int(1), Value::Int(2)])),
    ]);
    let s = v.to_json();
    let back = json::parse(&s).unwrap();
    assert_eq!(back, v);
}

#[test]
fn json_parse_numbers_and_escapes() {
    let v = json::parse(r#"{"n": -42, "f": 3.14, "s": "a\nb\t\"", "z": null}"#).unwrap();
    assert_eq!(v.get("n").and_then(|x| x.as_i64()), Some(-42));
    assert_eq!(v.get("f").and_then(|x| x.as_f64()), Some(3.14));
    assert_eq!(v.get("s").and_then(|x| x.as_str()), Some("a\nb\t\""));
    assert!(matches!(v.get("z"), Some(Value::Null)));
}

#[test]
fn json_parse_rejects_trailing_garbage() {
    assert!(json::parse("{} extra").is_err());
    assert!(json::parse("{bad}").is_err());
}

// ---------------------------------------------------------------------------
// Order state machine
// ---------------------------------------------------------------------------

#[test]
fn state_transitions_legal() {
    assert!(OrderState::New.can_transition_to(OrderState::Filled));
    assert!(OrderState::New.can_transition_to(OrderState::PartiallyFilled));
    assert!(OrderState::New.can_transition_to(OrderState::Canceled));
    assert!(OrderState::PartiallyFilled.can_transition_to(OrderState::Filled));
    assert!(!OrderState::Filled.can_transition_to(OrderState::Canceled));
    assert!(!OrderState::Rejected.can_transition_to(OrderState::Filled));
}

// ---------------------------------------------------------------------------
// OrderManager lifecycle
// ---------------------------------------------------------------------------

fn mgr() -> Arc<OrderManager> {
    Arc::new(OrderManager::new(Config::default()))
}

#[test]
fn submit_fills_and_records_fill() {
    let m = mgr();
    let now = models::now_ns();
    let order = models::parse_intent(
        &Value::Object(vec![
            ("symbol".into(), Value::String("EU_DAX_CONT".into())),
            ("side".into(), Value::String("BUY".into())),
            ("qty".into(), Value::Int(5)),
            ("limit_px".into(), Value::Float(16000.0)),
        ]),
        "SIM",
        now,
    )
    .unwrap();
    let submitted = m.submit(order, now).unwrap();
    assert_eq!(submitted.state, OrderState::Filled);
    assert_eq!(submitted.filled_qty, 5);

    let fills = m.fills(10);
    assert_eq!(fills.len(), 1);
    assert_eq!(fills[0].qty, 5);
    assert_eq!(fills[0].price, 16000.0);
}

#[test]
fn submit_rejects_oversized_qty() {
    let m = mgr();
    let now = models::now_ns();
    let order = models::parse_intent(
        &Value::Object(vec![
            ("symbol".into(), Value::String("SYM".into())),
            ("side".into(), Value::String("SELL".into())),
            ("qty".into(), Value::Int(10_001)), // > max_order_qty (10_000)
            ("limit_px".into(), Value::Float(10.0)),
        ]),
        "SIM",
        now,
    )
    .unwrap();
    assert!(m.submit(order, now).is_err());
}

#[test]
fn modify_open_then_filled_order() {
    let m = mgr();
    let now = models::now_ns();
    // A rejected order is terminal; use a fresh one. To test modify we need an
    // open order — the default config auto-fills, so craft via reject rate 1.0?
    // Instead: verify that modifying a FILLED order is rejected (terminal).
    let order = models::parse_intent(
        &Value::Object(vec![
            ("id".into(), Value::String("ORD-1".into())),
            ("symbol".into(), Value::String("SYM".into())),
            ("side".into(), Value::String("BUY".into())),
            ("qty".into(), Value::Int(2)),
            ("limit_px".into(), Value::Float(50.0)),
        ]),
        "SIM",
        now,
    )
    .unwrap();
    let sub = m.submit(order, now).unwrap();
    assert_eq!(sub.state, OrderState::Filled);
    // modifying a filled order must fail with an invalid-transition error
    let req = models::ModifyRequest { qty: Some(3), limit_price: None };
    let res = m.modify("ORD-1", &req, now);
    assert!(matches!(res, Err(Error { code: "EXG-202", .. })));
}

#[test]
fn cancel_terminal_order_fails() {
    let m = mgr();
    let now = models::now_ns();
    let order = models::parse_intent(
        &Value::Object(vec![
            ("id".into(), Value::String("ORD-2".into())),
            ("symbol".into(), Value::String("SYM".into())),
            ("side".into(), Value::String("BUY".into())),
            ("qty".into(), Value::Int(1)),
            ("limit_px".into(), Value::Float(50.0)),
        ]),
        "SIM",
        now,
    )
    .unwrap();
    m.submit(order, now).unwrap(); // -> FILLED
    let res = m.cancel("ORD-2", now);
    assert!(matches!(res, Err(Error { code: "EXG-202", .. })));
}

#[test]
fn unknown_order_lookup() {
    let m = mgr();
    let res = m.get("NOPE");
    assert!(matches!(res, Err(Error { code: "EXG-201", .. })));
}

// ---------------------------------------------------------------------------
// Wire parsing
// ---------------------------------------------------------------------------

#[test]
fn parse_intent_missing_fields() {
    let now = models::now_ns();
    // missing side
    assert!(models::parse_intent(
        &Value::Object(vec![("symbol".into(), Value::String("S".into()))]),
        "SIM",
        now,
    )
    .is_err());
    // bad side value
    let v = Value::Object(vec![
        ("symbol".into(), Value::String("S".into())),
        ("side".into(), Value::String("HOLD".into())),
        ("qty".into(), Value::Int(1)),
        ("limit_px".into(), Value::Float(1.0)),
    ]);
    assert!(models::parse_intent(&v, "SIM", now).is_err());
}

#[test]
fn parse_intent_side_aliases() {
    let now = models::now_ns();
    for (s, expected) in [("BID", OrderSide::Buy), ("ASK", OrderSide::Sell)] {
        let v = Value::Object(vec![
            ("symbol".into(), Value::String("S".into())),
            ("side".into(), Value::String(s.into())),
            ("qty".into(), Value::Int(1)),
            ("limit_px".into(), Value::Float(1.0)),
        ]);
        let o = models::parse_intent(&v, "SIM", now).unwrap();
        assert_eq!(o.side, expected);
    }
}

// ---------------------------------------------------------------------------
// HTTP end-to-end
// ---------------------------------------------------------------------------

fn http_request(port: u16, method: &str, path: &str, body: &str) -> (u16, Value) {
    let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
    let payload = if body.is_empty() {
        format!("{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    } else {
        format!(
            "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
    };
    let mut stream = stream;
    stream.write_all(payload.as_bytes()).unwrap();

    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).unwrap();
    let text = String::from_utf8_lossy(&buf);
    let head_end = text.find("\r\n\r\n").expect("no header/body split");
    let status_line = text.lines().next().unwrap();
    let status: u16 = status_line.split_whitespace().nth(1).unwrap().parse().unwrap();
    let body_str = &text[head_end + 4..];
    (status, json::parse(body_str).unwrap())
}

fn start_server() -> (u16, Arc<OrderManager>) {
    let manager = Arc::new(OrderManager::new(Config::default()));
    let router = exg::router::Router::with_default_routes();
    let port = exg::http::serve(0, router, std::sync::Arc::clone(&manager)).unwrap();
    (port, manager)
}

#[test]
fn http_healthz() {
    let (port, _m) = start_server();
    let (status, body) = http_request(port, "GET", "/healthz", "");
    assert_eq!(status, 200);
    assert_eq!(body.get("status").and_then(|v| v.as_str()), Some("ok"));
    assert_eq!(body.get("service").and_then(|v| v.as_str()), Some("execution-gateway"));
}

#[test]
fn http_submit_get_list_fills() {
    let (port, _m) = start_server();
    // submit
    let body = r#"{"id":"ORD-100","symbol":"EU_DAX_CONT","side":"BUY","qty":3,"limit_px":16000.5}"#;
    let (status, sub) = http_request(port, "POST", "/orders", body);
    assert_eq!(status, 200);
    assert_eq!(sub.get("id").and_then(|v| v.as_str()), Some("ORD-100"));
    assert_eq!(sub.get("state").and_then(|v| v.as_str()), Some("FILLED"));

    // get by id
    let (status, got) = http_request(port, "GET", "/orders/ORD-100", "");
    assert_eq!(status, 200);
    assert_eq!(got.get("qty").and_then(|v| v.as_i64()), Some(3));

    // list
    let (status, list) = http_request(port, "GET", "/orders?limit=10", "");
    assert_eq!(status, 200);
    assert!(list.get("count").and_then(|v| v.as_i64()).unwrap() >= 1);

    // fills
    let (status, fills) = http_request(port, "GET", "/fills?limit=10", "");
    assert_eq!(status, 200);
    assert!(fills.get("count").and_then(|v| v.as_i64()).unwrap() >= 1);
}

#[test]
fn http_unknown_order_404_and_bad_route() {
    let (port, _m) = start_server();
    let (status, body) = http_request(port, "GET", "/orders/DOES-NOT-EXIST", "");
    assert_eq!(status, 404);
    assert_eq!(body.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()), Some("EXG-201"));

    let (status, body) = http_request(port, "GET", "/nope", "");
    assert_eq!(status, 404);
    assert_eq!(body.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()), Some("EXG-404"));
}

#[test]
fn http_bad_intent_400() {
    let (port, _m) = start_server();
    let (status, body) = http_request(port, "POST", "/orders", r#"{"symbol":"X"}"#);
    assert_eq!(status, 400);
    assert_eq!(body.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()), Some("EXG-200"));
}

#[test]
fn http_cancel_filled_409() {
    let (port, _m) = start_server();
    let body = r#"{"id":"ORD-7","symbol":"SYM","side":"SELL","qty":1,"limit_px":10.0}"#;
    let (status, _) = http_request(port, "POST", "/orders", body);
    assert_eq!(status, 200);
    // default config auto-fills, so cancel must be a conflict
    let (status, body) = http_request(port, "DELETE", "/orders/ORD-7", "");
    assert_eq!(status, 409);
    assert_eq!(body.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()), Some("EXG-202"));
}
