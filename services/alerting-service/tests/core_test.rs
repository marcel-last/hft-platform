//! alerting_service (S10) — integration tests.
//!
//! Exercises the JSON codec, severity/state parsing, alert-event wire parsing,
//! dedup/suppression, escalation (via a clock-injection test), acknowledgement,
//! stats, and the full HTTP server end-to-end (submit / list / get / ack /
//! resolve / dispatch / stats / health / 404 / 403).  No external crates; a
//! tiny std `TcpStream` client is used for the HTTP round-trips.

use altsvc::config::Config;
use altsvc::core::{AlertManager, ManualClock, Stats};
use altsvc::errors::Error;
use altsvc::json::{self, Value};
use altsvc::models::{AlertEvent, AlertState, Severity};
use std::sync::Arc;
use std::io::{Read, Write};
use std::net::TcpStream;

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
// Severity / state parsing
// ---------------------------------------------------------------------------

#[test]
fn severity_roundtrip_and_synonyms() {
    for s in Severity::all() {
        assert_eq!(Severity::from_str(s.as_str()), Some(s));
    }
    assert_eq!(Severity::from_str("warn"), Some(Severity::Warning));
    assert_eq!(Severity::from_str("CRIT"), Some(Severity::Critical));
    assert_eq!(Severity::from_str("high"), Some(Severity::Critical));
    assert_eq!(Severity::from_str("low"), Some(Severity::Info));
    assert_eq!(Severity::from_str("bogus"), None);
}

#[test]
fn severity_ordering() {
    assert!(Severity::Info < Severity::Warning);
    assert!(Severity::Warning < Severity::Critical);
    assert!(!(Severity::Critical < Severity::Warning));
}

#[test]
fn alert_state_terminal_semantics() {
    assert!(!AlertState::Active.is_terminal());
    assert!(AlertState::Acknowledged.is_terminal());
    assert!(AlertState::Resolved.is_terminal());
}

// ---------------------------------------------------------------------------
// Alert event wire parsing
// ---------------------------------------------------------------------------

fn body(source: &str, category: &str, severity: &str) -> Value {
    Value::Object(vec![
        ("source".into(), Value::String(source.into())),
        ("category".into(), Value::String(category.into())),
        ("severity".into(), Value::String(severity.into())),
        ("message".into(), Value::String("msg".into())),
    ])
}

#[test]
fn parse_event_defaults() {
    let b = body("risk-manager", "kill_switch_engaged", "CRITICAL");
    let e = AlertEvent::parse(&b, 1234).unwrap();
    assert_eq!(e.source, "risk-manager");
    assert_eq!(e.category, "kill_switch_engaged");
    assert_eq!(e.severity, Severity::Critical);
    assert_eq!(e.ts_ns, 1234); // falls back to received time
    assert_eq!(e.received_ns, 1234);
    assert!(e.id.is_none());
}

#[test]
fn parse_event_with_context_and_ts() {
    let b = Value::Object(vec![
        ("source".into(), Value::String("latency-monitor".into())),
        ("category".into(), Value::String("budget_breach".into())),
        ("severity".into(), Value::String("WARNING".into())),
        (
            "context".into(),
            Value::Object(vec![("stage".into(), Value::String("order_to_ack".into()))]),
        ),
        ("ts_ns".into(), Value::Int(999)),
    ]);
    let e = AlertEvent::parse(&b, 500).unwrap();
    assert_eq!(e.ts_ns, 999); // explicit ts wins over received
    assert_eq!(
        e.context.get("stage").and_then(|v| v.as_str()),
        Some("order_to_ack")
    );
}

#[test]
fn parse_event_missing_source() {
    let b = Value::Object(vec![
        ("category".into(), Value::String("x".into())),
        ("severity".into(), Value::String("INFO".into())),
    ]);
    assert!(matches!(AlertEvent::parse(&b, 0), Err(Error { code: "ALT-200", .. })));
}

#[test]
fn parse_event_missing_category() {
    let b = Value::Object(vec![
        ("source".into(), Value::String("s".into())),
        ("severity".into(), Value::String("INFO".into())),
    ]);
    assert!(matches!(AlertEvent::parse(&b, 0), Err(Error { code: "ALT-200", .. })));
}

#[test]
fn parse_event_bad_severity() {
    let b = body("s", "c", "NOT_A_SEVERITY");
    assert!(matches!(AlertEvent::parse(&b, 0), Err(Error { code: "ALT-200", .. })));
}

#[test]
fn dedup_key_ignores_severity() {
    let a = AlertEvent::parse(&body("s", "c", "WARNING"), 0).unwrap();
    let b = AlertEvent::parse(&body("s", "c", "CRITICAL"), 0).unwrap();
    assert_eq!(a.dedup_key(), b.dedup_key()); // same key -> escalation, not new alert

    let d = AlertEvent::parse(
        &Value::Object(vec![
            ("source".into(), Value::String("s".into())),
            ("category".into(), Value::String("c".into())),
            ("severity".into(), Value::String("WARNING".into())),
            (
                "context".into(),
                Value::Object(vec![("k".into(), Value::Int(1))]),
            ),
        ]),
        0,
    )
    .unwrap();
    assert_ne!(a.dedup_key(), d.dedup_key()); // different context -> different key
}

// ---------------------------------------------------------------------------
// Config validation
// ---------------------------------------------------------------------------

#[test]
fn config_default_is_valid() {
    assert!(Config::default().validate().is_empty());
}

#[test]
fn config_validate_catches_bad_values() {
    let mut cfg = Config::default();
    cfg.listen_port = 0;
    cfg.dedup.suppression_window_ns = 1; // < dedup_window (30s)
    cfg.escalation.critical_escalate_after_ns = 1; // < warning (10 min)
    cfg.history.retained = 0;
    let errs = cfg.validate();
    assert!(errs.iter().any(|e| e.contains("listen_port")));
    assert!(errs.iter().any(|e| e.contains("suppression_window_ns")));
    assert!(errs.iter().any(|e| e.contains("critical_escalate_after_ns")));
    assert!(errs.iter().any(|e| e.contains("retained")));
}

#[test]
fn allows_source_open_mode_and_pinned() {
    let mut open = Config::default();
    open.allowed_sources.clear();
    assert!(open.allows_source("anything")); // open mode

    let pinned = Config::default();
    assert!(pinned.allows_source("risk-manager"));
    assert!(!pinned.allows_source("unknown-service"));
}

// ---------------------------------------------------------------------------
// Dedup / suppression (real clock, same-millisecond window)
// ---------------------------------------------------------------------------

fn manager_with(cfg: Config) -> Arc<AlertManager> {
    Arc::new(AlertManager::new(cfg))
}

/// A config that keeps the default 30s dedup window but clears the source list
/// (open mode) so tests can submit arbitrary sources.
fn open_cfg() -> Config {
    let mut cfg = Config::default();
    cfg.allowed_sources.clear();
    cfg
}

#[test]
fn first_submit_is_new_second_is_suppressed() {
    let m = manager_with(open_cfg());
    let e1 = AlertEvent::parse(&body("risk-manager", "kill_switch_engaged", "CRITICAL"), 0).unwrap();
    let o1 = m.submit(&e1).unwrap();
    assert!(o1.is_new);
    assert!(!o1.suppressed);
    assert_eq!(o1.occurrences, 1);

    // Same key immediately after -> folded into the same logical alert.
    let e2 = AlertEvent::parse(&body("risk-manager", "kill_switch_engaged", "CRITICAL"), 0).unwrap();
    let o2 = m.submit(&e2).unwrap();
    assert!(!o2.is_new);
    assert!(o2.suppressed);
    assert_eq!(o2.occurrences, 2);
    assert_eq!(o1.alert_id, o2.alert_id);

    // Stats reflect one new alert + one suppressed occurrence.
    let s = m.stats();
    assert_eq!(s.received_total, 2);
    assert_eq!(s.new_alerts_total, 1);
    assert_eq!(s.suppressed_total, 1);
}

#[test]
fn different_context_creates_new_alert() {
    let m = manager_with(open_cfg());
    let mk = |k: i64| {
        AlertEvent::parse(
            &Value::Object(vec![
                ("source".into(), Value::String("dqm".into())),
                ("category".into(), Value::String("feed_stale".into())),
                ("severity".into(), Value::String("WARNING".into())),
                (
                    "context".into(),
                    Value::Object(vec![("symbol".into(), Value::Int(k))]),
                ),
            ]),
            0,
        )
        .unwrap()
    };
    let o1 = m.submit(&mk(1)).unwrap();
    let o2 = m.submit(&mk(2)).unwrap(); // different context -> new logical alert
    assert!(o1.is_new);
    assert!(o2.is_new);
    assert_ne!(o1.alert_id, o2.alert_id);
    assert_eq!(m.alert_count(), 2);
}

#[test]
fn severity_bump_on_fold_raises_effective_severity() {
    let m = manager_with(open_cfg());
    let warn = AlertEvent::parse(&body("s", "c", "WARNING"), 0).unwrap();
    let o1 = m.submit(&warn).unwrap();
    assert_eq!(o1.severity, Severity::Warning);

    // Same key, higher severity -> folded AND effective severity rises.
    let crit = AlertEvent::parse(&body("s", "c", "CRITICAL"), 0).unwrap();
    let o2 = m.submit(&crit).unwrap();
    assert!(o2.suppressed);
    assert_eq!(o2.severity, Severity::Critical);

    let a = m.get(&o1.alert_id).unwrap();
    assert_eq!(a.base_severity, Severity::Warning); // original preserved
    assert_eq!(a.current_severity, Severity::Critical); // effective rose
}

#[test]
fn unauthorized_source_rejected() {
    let cfg = Config::default(); // pinned to the three known sources
    let m = manager_with(cfg);
    let e = AlertEvent::parse(&body("rogue-service", "c", "INFO"), 0).unwrap();
    assert!(matches!(m.submit(&e), Err(Error { code: "ALT-201", .. })));
}

// ---------------------------------------------------------------------------
// Escalation (clock injection)
// ---------------------------------------------------------------------------

#[test]
fn warning_escalates_to_critical_after_threshold() {
    let mut cfg = open_cfg();
    // Warning escalates to CRITICAL after 10 min; on-call never in this test.
    cfg.escalation.warning_escalate_after_ns = 10 * 60_000_000_000;
    cfg.escalation.critical_escalate_after_ns = i64::MAX;

    let clock = Arc::new(ManualClock::new(1_000));
    let m = AlertManager::with_clock(cfg, Box::new(clock.clone()));

    let e = AlertEvent::parse(&body("s", "c", "WARNING"), 0).unwrap();
    let o1 = m.submit(&e).unwrap(); // t=1000, age 0 -> WARNING
    assert_eq!(o1.severity, Severity::Warning);

    // Just under the threshold: still WARNING.
    clock.set(1_000 + 9 * 60_000_000_000); // age = 9 min < 10 min
    assert_eq!(m.get(&o1.alert_id).unwrap().current_severity, Severity::Warning);

    // Past the threshold: escalates to CRITICAL.
    clock.set(1_000 + 11 * 60_000_000_000); // age = 11 min >= 10 min
    assert_eq!(m.get(&o1.alert_id).unwrap().current_severity, Severity::Critical);
}

#[test]
fn acknowledged_alert_does_not_escalate() {
    let mut cfg = open_cfg();
    // Would escalate almost instantly if the alert were still active.
    cfg.escalation.warning_escalate_after_ns = 1_000_000; // 1 ms
    cfg.escalation.critical_escalate_after_ns = i64::MAX;

    let clock = Arc::new(ManualClock::new(1_000));
    let m = AlertManager::with_clock(cfg, Box::new(clock.clone()));

    let e = AlertEvent::parse(&body("s", "c", "WARNING"), 0).unwrap();
    let o1 = m.submit(&e).unwrap(); // t=1000, age 0 -> WARNING
    assert_eq!(o1.severity, Severity::Warning);

    // Acknowledge at t=2000 (age 1000 ns < 1 ms threshold): frozen as WARNING.
    clock.set(2_000);
    let ticket = m.acknowledge(&o1.alert_id).unwrap();
    assert_eq!(ticket.state, AlertState::Acknowledged);
    assert_eq!(ticket.severity, Severity::Warning);

    // Advance far past the threshold: an active alert would be CRITICAL now,
    // but this one is acknowledged and frozen at WARNING.
    clock.set(1_000 + 60_000_000_000); // 1 min later
    let a = m.get(&o1.alert_id).unwrap();
    assert_eq!(a.current_severity, Severity::Warning);
    assert_eq!(a.state, AlertState::Acknowledged);
}

#[test]
fn resubmit_after_ack_opens_fresh_alert() {
    let m = manager_with(open_cfg());
    let e = AlertEvent::parse(&body("s", "c", "WARNING"), 0).unwrap();
    let o1 = m.submit(&e).unwrap();
    m.acknowledge(&o1.alert_id).unwrap(); // close it

    // A new occurrence for the same key must NOT reopen the acked alert;
    // it opens a fresh logical alert instead.
    let o2 = m.submit(&e).unwrap();
    assert!(o2.is_new);
    assert_ne!(o1.alert_id, o2.alert_id);

    // The original is closed (resolved) and its severity frozen at ack time;
    // the new one is active.
    let old = m.get(&o1.alert_id).unwrap();
    assert_eq!(old.state, AlertState::Resolved);
    assert_eq!(old.current_severity, Severity::Warning); // frozen, not escalated
    assert_eq!(m.get(&o2.alert_id).unwrap().state, AlertState::Active);
}

#[test]
fn oncall_flag_set_on_aged_critical() {
    let mut cfg = open_cfg();
    cfg.escalation.warning_escalate_after_ns = i64::MAX; // never auto-escalate WARNING
    cfg.escalation.critical_escalate_after_ns = 15 * 60_000_000_000; // 15 min

    let clock = Arc::new(ManualClock::new(1_000));
    let m = AlertManager::with_clock(cfg, Box::new(clock.clone()));

    let e = AlertEvent::parse(&body("s", "c", "CRITICAL"), 0).unwrap();
    let o1 = m.submit(&e).unwrap(); // t=1000, age 0 -> not on-call yet
    assert!(!m.get(&o1.alert_id).unwrap().needs_oncall);

    // Just under: still not on-call.
    clock.set(1_000 + 14 * 60_000_000_000); // age 14 min < 15 min
    assert!(!m.get(&o1.alert_id).unwrap().needs_oncall);

    // Past the threshold: flagged for on-call.
    clock.set(1_000 + 16 * 60_000_000_000); // age 16 min >= 15 min
    assert!(m.get(&o1.alert_id).unwrap().needs_oncall);
}

// ---------------------------------------------------------------------------
// Acknowledgement / resolve
// ---------------------------------------------------------------------------

#[test]
fn ack_unknown_alert_is_404() {
    let m = manager_with(open_cfg());
    assert!(matches!(m.acknowledge("ALT-99999999"), Err(Error { code: "ALT-202", .. })));
}

#[test]
fn ack_then_resolve_flow() {
    let m = manager_with(open_cfg());
    let e = AlertEvent::parse(&body("s", "c", "INFO"), 0).unwrap();
    let o1 = m.submit(&e).unwrap();

    let t1 = m.acknowledge(&o1.alert_id).unwrap();
    assert_eq!(t1.state, AlertState::Acknowledged);

    // Re-ack of an acknowledged alert is idempotent (still ACKNOWLEDGED).
    let t2 = m.acknowledge(&o1.alert_id).unwrap();
    assert_eq!(t2.state, AlertState::Acknowledged);

    // Resolve closes it; re-resolve is idempotent.
    assert_eq!(m.resolve(&o1.alert_id).unwrap(), AlertState::Resolved);
    assert_eq!(m.resolve(&o1.alert_id).unwrap(), AlertState::Resolved);

    let s = m.stats();
    assert_eq!(s.acks_total, 2);
}

#[test]
fn ack_resolved_alert_is_409() {
    let m = manager_with(open_cfg());
    let e = AlertEvent::parse(&body("s", "c", "INFO"), 0).unwrap();
    let o1 = m.submit(&e).unwrap();
    m.resolve(&o1.alert_id).unwrap(); // close it first
    assert!(matches!(m.acknowledge(&o1.alert_id), Err(Error { code: "ALT-203", .. })));
}

#[test]
fn resolve_unknown_alert_is_404() {
    let m = manager_with(open_cfg());
    assert!(matches!(m.resolve("ALT-99999999"), Err(Error { code: "ALT-202", .. })));
}

// ---------------------------------------------------------------------------
// List / stats / dispatch log
// ---------------------------------------------------------------------------

#[test]
fn list_filters_by_source_and_state() {
    let m = manager_with(open_cfg());
    let a = AlertEvent::parse(&body("risk-manager", "a", "CRITICAL"), 0).unwrap();
    let b = AlertEvent::parse(&body("latency-monitor", "b", "WARNING"), 0).unwrap();
    m.submit(&a).unwrap();
    m.submit(&b).unwrap();

    assert_eq!(m.list(Some("risk-manager"), None, None, 0).len(), 1);
    assert_eq!(m.list(None, Some(AlertState::Active), None, 0).len(), 2);
    assert_eq!(m.list(None, None, Some(Severity::Critical), 0).len(), 1);

    // Limit bounds the result.
    assert_eq!(m.list(None, None, None, 1).len(), 1);
}

#[test]
fn dispatch_log_records_new_alerts() {
    let m = manager_with(open_cfg());
    let a = AlertEvent::parse(&body("risk-manager", "a", "CRITICAL"), 0).unwrap();
    let b = AlertEvent::parse(&body("dqm", "b", "WARNING"), 0).unwrap();
    m.submit(&a).unwrap();
    m.submit(&b).unwrap();
    // Each new alert produces one dispatch entry.
    assert_eq!(m.dispatch_log(0).len(), 2);
    // Newest first: the second submit is at index 0.
    let newest = &m.dispatch_log(0)[0];
    assert_eq!(
        newest.get("category").and_then(|v| v.as_str()),
        Some("b")
    );
}

#[test]
fn stats_counters_are_consistent() {
    let m = manager_with(open_cfg());
    let e = AlertEvent::parse(&body("s", "c", "INFO"), 0).unwrap();
    for _ in 0..3 {
        m.submit(&e).unwrap(); // 1 new + 2 suppressed (same key)
    }
    let s: Stats = m.stats();
    assert_eq!(s.received_total, 3);
    assert_eq!(s.new_alerts_total, 1);
    assert_eq!(s.suppressed_total, 2);
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

fn start_server(cfg: Config) -> (u16, Arc<AlertManager>) {
    let manager = Arc::new(AlertManager::new(cfg));
    let router = altsvc::router::Router::with_default_routes();
    let port = altsvc::http::serve(0, router, std::sync::Arc::clone(&manager)).unwrap();
    (port, manager)
}

#[test]
fn http_healthz() {
    let (port, _m) = start_server(open_cfg());
    let (status, body) = http_request(port, "GET", "/healthz", "");
    assert_eq!(status, 200);
    assert_eq!(body.get("status").and_then(|v| v.as_str()), Some("ok"));
    assert_eq!(
        body.get("service").and_then(|v| v.as_str()),
        Some("alerting-service")
    );
}

#[test]
fn http_readyz() {
    let (port, _m) = start_server(open_cfg());
    let (status, body) = http_request(port, "GET", "/readyz", "");
    assert_eq!(status, 200);
    assert_eq!(body.get("status").and_then(|v| v.as_str()), Some("ready"));
}

#[test]
fn http_submit_new_and_suppressed() {
    let (port, _m) = start_server(open_cfg());
    let body = r#"{"source":"risk-manager","category":"kill_switch_engaged","severity":"CRITICAL","message":"engaged"}"#;
    let (status, resp) = http_request(port, "POST", "/alerts", body);
    assert_eq!(status, 200);
    let outcome = resp.get("outcome").expect("outcome");
    assert_eq!(outcome.get("is_new").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(outcome.get("occurrences").and_then(|v| v.as_i64()), Some(1));

    // Same key again -> suppressed.
    let (status, resp2) = http_request(port, "POST", "/alerts", body);
    assert_eq!(status, 200);
    let outcome2 = resp2.get("outcome").expect("outcome");
    assert_eq!(outcome2.get("suppressed").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(outcome2.get("occurrences").and_then(|v| v.as_i64()), Some(2));

    // /alerts now lists exactly one logical alert.
    let (status, list) = http_request(port, "GET", "/alerts", "");
    assert_eq!(status, 200);
    assert_eq!(list.get("count").and_then(|v| v.as_i64()), Some(1));
}

#[test]
fn http_get_by_id_and_404() {
    let (port, _m) = start_server(open_cfg());
    let body = r#"{"source":"dqm","category":"feed_stale","severity":"WARNING"}"#;
    let (_, resp) = http_request(port, "POST", "/alerts", body);
    let id = resp
        .get("outcome")
        .and_then(|o| o.get("alert_id"))
        .and_then(|v| v.as_str())
        .unwrap()
        .to_string();

    let (status, got) = http_request(port, "GET", &format!("/alerts/{id}"), "");
    assert_eq!(status, 200);
    assert_eq!(got.get("category").and_then(|v| v.as_str()), Some("feed_stale"));

    // Unknown id -> ALT-202 (404).
    let (status, err) = http_request(port, "GET", "/alerts/ALT-99999999", "");
    assert_eq!(status, 404);
    assert_eq!(
        err.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("ALT-202")
    );
}

#[test]
fn http_ack_flow() {
    let (port, _m) = start_server(open_cfg());
    let body = r#"{"source":"risk-manager","category":"breach","severity":"CRITICAL"}"#;
    let (_, resp) = http_request(port, "POST", "/alerts", body);
    let id = resp
        .get("outcome")
        .and_then(|o| o.get("alert_id"))
        .and_then(|v| v.as_str())
        .unwrap()
        .to_string();

    // Acknowledge.
    let (status, ack) = http_request(port, "POST", "/alerts/ack", &format!(r#"{{"alert_id":"{id}"}}"#));
    assert_eq!(status, 200);
    assert_eq!(
        ack.get("ticket").and_then(|t| t.get("state")).and_then(|v| v.as_str()),
        Some("ACKNOWLEDGED")
    );

    // Ack of an unknown id -> ALT-202 (404).
    let (status, err) = http_request(port, "POST", "/alerts/ack", r#"{"alert_id":"ALT-99999999"}"#);
    assert_eq!(status, 404);
    assert_eq!(
        err.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("ALT-202")
    );
}

#[test]
fn http_resolve_flow() {
    let (port, _m) = start_server(open_cfg());
    let body = r#"{"source":"dqm","category":"x","severity":"INFO"}"#;
    let (_, resp) = http_request(port, "POST", "/alerts", body);
    let id = resp
        .get("outcome")
        .and_then(|o| o.get("alert_id"))
        .and_then(|v| v.as_str())
        .unwrap()
        .to_string();

    let (status, r) = http_request(port, "POST", &format!("/alerts/{id}/resolve"), "");
    assert_eq!(status, 200);
    assert_eq!(r.get("state").and_then(|v| v.as_str()), Some("RESOLVED"));

    // Re-resolve is idempotent.
    let (status, r2) = http_request(port, "POST", &format!("/alerts/{id}/resolve"), "");
    assert_eq!(status, 200);
    assert_eq!(r2.get("state").and_then(|v| v.as_str()), Some("RESOLVED"));

    // Acking a resolved alert -> ALT-203 (409).
    let (status, err) = http_request(port, "POST", "/alerts/ack", &format!(r#"{{"alert_id":"{id}"}}"#));
    assert_eq!(status, 409);
    assert_eq!(
        err.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("ALT-203")
    );
}

#[test]
fn http_unauthorized_source_403() {
    // Pinned allow-list (default) rejects unknown sources.
    let (port, _m) = start_server(Config::default());
    let body = r#"{"source":"rogue","category":"c","severity":"INFO"}"#;
    let (status, err) = http_request(port, "POST", "/alerts", body);
    assert_eq!(status, 403);
    assert_eq!(
        err.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("ALT-201")
    );
}

#[test]
fn http_bad_request_400() {
    let (port, _m) = start_server(open_cfg());
    // Missing source -> ALT-200.
    let (status, err) = http_request(port, "POST", "/alerts", r#"{"category":"c","severity":"INFO"}"#);
    assert_eq!(status, 400);
    assert_eq!(
        err.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("ALT-200")
    );

    // Malformed JSON -> ALT-200.
    let (status, err) = http_request(port, "POST", "/alerts", "{not json");
    assert_eq!(status, 400);
    assert_eq!(
        err.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("ALT-200")
    );

    // Empty body -> ALT-200.
    let (status, _err) = http_request(port, "POST", "/alerts", "");
    assert_eq!(status, 400);
}

#[test]
fn http_dispatch_and_stats() {
    let (port, _m) = start_server(open_cfg());
    for src in ["risk-manager", "dqm"] {
        let body = format!(r#"{{"source":"{src}","category":"c","severity":"WARNING"}}"#);
        http_request(port, "POST", "/alerts", &body).0;
    }

    let (status, d) = http_request(port, "GET", "/alerts/dispatch", "");
    assert_eq!(status, 200);
    assert_eq!(d.get("count").and_then(|v| v.as_i64()), Some(2));

    let (status, s) = http_request(port, "GET", "/stats", "");
    assert_eq!(status, 200);
    assert_eq!(s.get("received_total").and_then(|v| v.as_i64()), Some(2));
    assert_eq!(s.get("new_alerts_total").and_then(|v| v.as_i64()), Some(2));
}

#[test]
fn http_unknown_route_404() {
    let (port, _m) = start_server(open_cfg());
    let (status, err) = http_request(port, "GET", "/nope", "");
    assert_eq!(status, 404);
    assert_eq!(
        err.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()),
        Some("ALT-404")
    );
}
