//! latency_monitor (S7) — integration tests.
//!
//! Exercises the JSON codec, stage/timestamp parsing, rolling-window percentile
//! math, budget-breach alerting (with cooldown), and the full HTTP server
//! end-to-end (stamp / latency / percentiles / budgets / health / 404).
//! No external crates; a tiny std `TcpStream` client is used for the HTTP
//! round-trips.

use latmon::config::Config;
use latmon::core::LatencyMonitor;
use latmon::errors::Error;
use latmon::json::{self, Value};
use latmon::models::{self, LatencySample, Stage};
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
// Stage + sample parsing
// ---------------------------------------------------------------------------

#[test]
fn stage_names_roundtrip() {
    for s in Stage::all() {
        let name = s.as_str();
        assert_eq!(Stage::from_str(name), Some(s));
    }
    assert_eq!(Stage::from_str("venue_to_gateway"), Some(Stage::VenueToGateway));
    assert_eq!(Stage::from_str("ORDER_TO_ACK"), Some(Stage::OrderToAck)); // case-insensitive
    assert_eq!(Stage::from_str("bogus_stage"), None);
}

#[test]
fn stage_timestamp_pairs() {
    assert_eq!(Stage::VenueToGateway.timestamp_pair(), Some(("vt", "rt")));
    assert_eq!(Stage::GatewayToBook.timestamp_pair(), Some(("rt", "bt")));
    assert_eq!(Stage::BookToSignal.timestamp_pair(), Some(("bt", "st")));
    assert_eq!(Stage::SignalToOrder.timestamp_pair(), Some(("st", "ot")));
    assert_eq!(Stage::OrderToAck.timestamp_pair(), Some(("ot", "at")));
}

#[test]
fn parse_stamp_from_timestamp_pair() {
    let now = models::now_ns();
    let body = Value::Object(vec![
        ("stage".into(), Value::String("venue_to_gateway".into())),
        ("vt".into(), Value::Int(1_000)),
        ("rt".into(), Value::Int(1_050)),
        ("symbol".into(), Value::String("EU_DAX_CONT".into())),
    ]);
    let s = LatencySample::parse(&body, now).unwrap();
    assert_eq!(s.stage, Stage::VenueToGateway);
    assert_eq!(s.duration_ns, 50);
    assert_eq!(s.t0_ns, 1_000);
    assert_eq!(s.t1_ns, 1_050);
    assert_eq!(s.symbol.as_deref(), Some("EU_DAX_CONT"));
}

#[test]
fn parse_stamp_from_explicit_duration() {
    let now = models::now_ns();
    let body = Value::Object(vec![
        ("stage".into(), Value::String("order_to_ack".into())),
        ("duration_ns".into(), Value::Int(123_456)),
    ]);
    let s = LatencySample::parse(&body, now).unwrap();
    assert_eq!(s.stage, Stage::OrderToAck);
    assert_eq!(s.duration_ns, 123_456);
}

#[test]
fn parse_stamp_rejects_inverted_pair() {
    let body = Value::Object(vec![
        ("stage".into(), Value::String("gateway_to_book".into())),
        ("rt".into(), Value::Int(100)),
        ("bt".into(), Value::Int(50)), // t1 < t0
    ]);
    let res = LatencySample::parse(&body, 0);
    assert!(matches!(res, Err(Error { code: "LAT-202", .. })));
}

#[test]
fn parse_stamp_rejects_negative_duration() {
    let body = Value::Object(vec![
        ("stage".into(), Value::String("book_to_signal".into())),
        ("duration_ns".into(), Value::Int(-5)),
    ]);
    let res = LatencySample::parse(&body, 0);
    assert!(matches!(res, Err(Error { code: "LAT-203", .. })));
}

#[test]
fn parse_stamp_missing_stage() {
    let body = Value::Object(vec![("duration_ns".into(), Value::Int(10))]);
    let res = LatencySample::parse(&body, 0);
    assert!(matches!(res, Err(Error { code: "LAT-200", .. })));
}

#[test]
fn parse_stamp_missing_pair_field() {
    let body = Value::Object(vec![
        ("stage".into(), Value::String("signal_to_order".into())),
        // only st present; ot missing
        ("st".into(), Value::Int(10)),
    ]);
    let res = LatencySample::parse(&body, 0);
    assert!(matches!(res, Err(Error { code: "LAT-200", .. })));
}

// ---------------------------------------------------------------------------
// Percentile math
// ---------------------------------------------------------------------------

fn monitor_with(cfg: Config) -> Arc<LatencyMonitor> {
    Arc::new(LatencyMonitor::new(cfg))
}

/// A config with a tiny window and min_samples so tests stay fast.
fn small_cfg() -> Config {
    let mut cfg = Config::default();
    cfg.window.window_size = 1_000;
    cfg.window.min_samples = 2;
    // No budgets by default in this helper unless overridden below.
    cfg.budget.stage_budgets_ns.clear();
    cfg
}

fn stamp(m: &LatencyMonitor, stage: Stage, d: i64) {
    let body = Value::Object(vec![
        ("stage".into(), Value::String(stage.as_str().to_string())),
        ("duration_ns".into(), Value::Int(d)),
    ]);
    let s = LatencySample::parse(&body, models::now_ns()).unwrap();
    m.record(&s).unwrap();
}

#[test]
fn percentiles_exact_on_uniform_window() {
    let m = monitor_with(small_cfg());
    // 1..=100 -> p50 should be ~50.5, p99 ~99.01, min 1, max 100.
    for d in 1..=100i64 {
        stamp(&m, Stage::VenueToGateway, d);
    }
    let st = m.stage_stats(Stage::VenueToGateway, false).unwrap();
    assert_eq!(st.count, 100);
    assert_eq!(st.min_ns, Some(1));
    assert_eq!(st.max_ns, Some(100));
    assert_eq!(st.mean_ns, Some(50)); // (1+..+100)/100 = 50.5 -> truncates to 50
    // p50 via linear interpolation of sorted [1..=100]: rank=(99)*0.5=49.5 -> 50 + 0.5*(51-50)=50.5 -> 50
    assert_eq!(st.p50_ns, Some(50));
    // p99: rank = 99*0.99 = 98.01 -> floor 98, frac ~0.01*1000=10 -> 99 + (100-99)*10/1000 = 99
    assert_eq!(st.p99_ns, Some(99));
}

#[test]
fn percentile_empty_window_is_none() {
    let m = monitor_with(small_cfg());
    assert_eq!(m.percentile(Stage::OrderToAck, 99.0), None);
}

#[test]
fn window_evicts_oldest_beyond_capacity() {
    let mut cfg = small_cfg();
    cfg.window.window_size = 10;
    let m = monitor_with(cfg);
    // Push 25 samples of duration 1, then one sample of duration 1_000_000.
    for _ in 0..25 {
        stamp(&m, Stage::GatewayToBook, 1);
    }
    stamp(&m, Stage::GatewayToBook, 1_000_000);
    let st = m.stage_stats(Stage::GatewayToBook, false).unwrap();
    assert_eq!(st.count, 10); // window capped at 10
    assert_eq!(st.total, 26); // monotonic total keeps counting
    assert_eq!(st.max_ns, Some(1_000_000));
}

#[test]
fn insufficient_samples_yields_lat_204() {
    let m = monitor_with(small_cfg()); // min_samples = 2
    stamp(&m, Stage::BookToSignal, 5); // only 1 sample
    let res = m.stage_stats(Stage::BookToSignal, true);
    assert!(matches!(res, Err(Error { code: "LAT-204", .. })));
    // With require_min=false it succeeds with a count of 1.
    let ok = m.stage_stats(Stage::BookToSignal, false).unwrap();
    assert_eq!(ok.count, 1);
}

// ---------------------------------------------------------------------------
// Budget breach + alerting
// ---------------------------------------------------------------------------

#[test]
fn budget_breach_fires_alert_with_cooldown() {
    let mut cfg = small_cfg();
    // Tight budget so a single large sample breaches: p99 of one sample == the sample.
    cfg.budget.stage_budgets_ns = vec![("order_to_ack".to_string(), 100)];
    cfg.budget.breach_ratio = 1.25; // threshold = 125 ns
    cfg.budget.alert_cooldown_ns = i64::MAX; // never expires -> at most one alert
    let m = monitor_with(cfg);

    // First breach: p99 (of a single sample) = 1_000 > 125 -> alert.
    let body = Value::Object(vec![
        ("stage".into(), Value::String("order_to_ack".into())),
        ("duration_ns".into(), Value::Int(1_000)),
    ]);
    let s = LatencySample::parse(&body, models::now_ns()).unwrap();
    let a1 = m.record(&s).unwrap();
    assert!(a1.is_some());
    let a1 = a1.unwrap();
    assert_eq!(a1.stage, Stage::OrderToAck);
    assert_eq!(a1.observed_p99_ns, 1_000);
    assert_eq!(a1.budget_ns, 100);
    // pct_over = (1000-100)*100/100 = 900
    assert_eq!(a1.pct_over, 900);
    assert_eq!(a1.severity, latmon::models::AlertSeverity::Critical); // 1000 >= 2*100

    // Second breach within cooldown (i64::MAX) -> suppressed.
    let a2 = m.record(&s).unwrap();
    assert!(a2.is_none());

    // Alert history holds exactly one.
    assert_eq!(m.alert_count(), 1);
}

#[test]
fn no_breach_when_under_threshold() {
    let mut cfg = small_cfg();
    cfg.budget.stage_budgets_ns = vec![("venue_to_gateway".to_string(), 1_000)];
    cfg.budget.breach_ratio = 1.25; // threshold 1250
    let m = monitor_with(cfg);
    for d in [10, 20, 30] {
        stamp(&m, Stage::VenueToGateway, d);
    }
    let st = m.stage_stats(Stage::VenueToGateway, false).unwrap();
    assert!(!st.breached);
    assert_eq!(m.alert_count(), 0);
}

#[test]
fn budgets_listing_matches_config() {
    let m = monitor_with(Config::default());
    let b = m.budgets();
    assert_eq!(b.len(), 5);
    assert_eq!(b[0].stage, Stage::VenueToGateway);
    assert_eq!(b[0].budget_ns, 50_000);
}

// ---------------------------------------------------------------------------
// Config validation
// ---------------------------------------------------------------------------

#[test]
fn config_validate_catches_bad_values() {
    let mut cfg = Config::default();
    cfg.listen_port = 0;
    cfg.window.min_samples = 1; // < 2
    cfg.budget.breach_ratio = 0.5; // < 1.0
    let errs = cfg.validate();
    assert!(errs.iter().any(|e| e.contains("listen_port")));
    assert!(errs.iter().any(|e| e.contains("min_samples")));
    assert!(errs.iter().any(|e| e.contains("breach_ratio")));
}

#[test]
fn config_default_is_valid() {
    assert!(Config::default().validate().is_empty());
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

fn start_server() -> (u16, Arc<LatencyMonitor>) {
    let monitor = Arc::new(LatencyMonitor::new(Config::default()));
    let router = latmon::router::Router::with_default_routes();
    let port = latmon::http::serve(0, router, std::sync::Arc::clone(&monitor)).unwrap();
    (port, monitor)
}

#[test]
fn http_healthz() {
    let (port, _m) = start_server();
    let (status, body) = http_request(port, "GET", "/healthz", "");
    assert_eq!(status, 200);
    assert_eq!(body.get("status").and_then(|v| v.as_str()), Some("ok"));
    assert_eq!(
        body.get("service").and_then(|v| v.as_str()),
        Some("latency-monitor")
    );
}

#[test]
fn http_readyz() {
    let (port, _m) = start_server();
    let (status, body) = http_request(port, "GET", "/readyz", "");
    assert_eq!(status, 200);
    assert_eq!(body.get("status").and_then(|v| v.as_str()), Some("ready"));
}

#[test]
fn http_stamp_and_latency() {
    let (port, _m) = start_server();
    // Stamp a venue_to_gateway sample via the raw timestamp pair.
    let body = r#"{"stage":"venue_to_gateway","vt":1000,"rt":1060,"symbol":"EU_DAX_CONT"}"#;
    let (status, resp) = http_request(port, "POST", "/stamp", body);
    assert_eq!(status, 200);
    let sample = resp.get("sample").expect("sample field");
    assert_eq!(
        sample.get("duration_ns").and_then(|v| v.as_i64()),
        Some(60)
    );

    // /latency now reports one stage table row with count >= 1 for that stage.
    let (status, lat) = http_request(port, "GET", "/latency", "");
    assert_eq!(status, 200);
    assert_eq!(lat.get("count").and_then(|v| v.as_i64()), Some(5));
}

#[test]
fn http_latency_stage_min_gate() {
    let (port, _m) = start_server();
    // Default min_samples is 16; with zero samples the strict route must 409.
    let (status, body) = http_request(port, "GET", "/latency/order_to_ack", "");
    assert_eq!(status, 409);
    assert_eq!(
        body.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("LAT-204")
    );
    // Relaxed with ?min=false -> 200 with count 0.
    let (status, body) = http_request(port, "GET", "/latency/order_to_ack?min=false", "");
    assert_eq!(status, 200);
    assert_eq!(body.get("count").and_then(|v| v.as_i64()), Some(0));
}

#[test]
fn http_unknown_stage_404_and_bad_route() {
    let (port, _m) = start_server();
    let (status, body) = http_request(port, "GET", "/latency/not_a_stage", "");
    assert_eq!(status, 404);
    assert_eq!(
        body.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("LAT-201")
    );

    let (status, body) = http_request(port, "GET", "/nope", "");
    assert_eq!(status, 404);
    assert_eq!(
        body.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("LAT-404")
    );
}

#[test]
fn http_bad_stamp_400() {
    let (port, _m) = start_server();
    // Inverted timestamps -> LAT-202 (400).
    let body = r#"{"stage":"gateway_to_book","rt":100,"bt":50}"#;
    let (status, resp) = http_request(port, "POST", "/stamp", body);
    assert_eq!(status, 400);
    assert_eq!(
        resp.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("LAT-202")
    );

    // Missing stage -> LAT-200 (400).
    let (status, resp) = http_request(port, "POST", "/stamp", r#"{"duration_ns":10}"#);
    assert_eq!(status, 400);
    assert_eq!(
        resp.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("LAT-200")
    );

    // Malformed JSON -> LAT-200 (400).
    let (status, resp) = http_request(port, "POST", "/stamp", "{not json");
    assert_eq!(status, 400);
    assert_eq!(
        resp.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("LAT-200")
    );
}

#[test]
fn http_percentiles_and_budgets() {
    let (port, _m) = start_server();
    let (status, p) = http_request(port, "GET", "/percentiles", "");
    assert_eq!(status, 200);
    assert_eq!(p.get("count").and_then(|v| v.as_i64()), Some(5));

    let (status, b) = http_request(port, "GET", "/budgets", "");
    assert_eq!(status, 200);
    assert_eq!(b.get("count").and_then(|v| v.as_i64()), Some(5));
}

#[test]
fn http_stamp_breach_returns_alert() {
    let (port, _m) = start_server();
    // Default order_to_ack budget is 1_000_000 ns with breach_ratio 1.25
    // (threshold 1_250_000). A single 5_000_000 ns sample makes p99 = 5e6 > threshold.
    let body = r#"{"stage":"order_to_ack","duration_ns":5000000}"#;
    let (status, resp) = http_request(port, "POST", "/stamp", body);
    assert_eq!(status, 200);
    let alert = resp.get("alert").expect("alert field");
    assert!(!matches!(alert, Value::Null));
    assert_eq!(
        alert.get("stage").and_then(|v| v.as_str()),
        Some("order_to_ack")
    );
}
