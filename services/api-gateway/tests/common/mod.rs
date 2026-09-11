//! Shared fixtures for the api-gateway test suite.
//!
//! The `Transport` trait (`core.rs`) is the testability seam: production is
//! `HttpTransport` (real sockets via the template client), tests inject a
//! `FakeTransport` returning a canned `(status, Value)` per `(addr, method,
//! path)` and recording every call so tests can assert exactly what the gateway
//! forwarded upstream (path rewrite, query, body, target port).
//!
//! Fixtures are faithful to the **real S12** `/verify` wire: a 2xx success is
//! a FLAT claims object (no nested `claims` wrapper) and a 4xx rejection
//! carries S12's `error.code` (+ optional `context.reason`).
//!
//! Some fixtures are used by only a subset of the test binaries, so shared
//! dead code is expected and allowed here.
#![allow(dead_code)]

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use apigw::config::GatewayConfig;
use apigw::core::{Outbound, OutboundResult, Transport};
use apigw::errors::ApiError;
use apigw::json::Value;

/// One outbound hop the fake transport observed.
#[derive(Debug, Clone)]
pub struct FakeCall {
    pub addr: (String, u16),
    pub method: String,
    pub path: String,
    pub body: Option<Value>,
}

/// Scripted transport: canned response per call + a recording log.
pub struct FakeTransport {
    pub calls: Mutex<Vec<FakeCall>>,
    responder: Arc<dyn Fn(&FakeCall) -> Result<OutboundResult, ApiError> + Send + Sync>,
}

impl FakeTransport {
    /// Wrap a responder closure; the returned `Arc` is both the `Transport`
    /// and the call-recording handle tests read back.
    pub fn new(
        responder: impl Fn(&FakeCall) -> Result<OutboundResult, ApiError> + Send + Sync + 'static,
    ) -> Arc<Self> {
        Arc::new(FakeTransport { calls: Mutex::new(Vec::new()), responder: Arc::new(responder) })
    }
    /// All calls recorded so far (cloned snapshot).
    pub fn calls(&self) -> Vec<FakeCall> {
        self.calls.lock().unwrap().clone()
    }
    /// Calls that targeted a specific upstream port (7720=S12, 7690=S9, 7740=S14).
    pub fn to_port(&self, port: u16) -> Vec<FakeCall> {
        self.calls().into_iter().filter(|c| c.addr.1 == port).collect()
    }
}

impl Transport for FakeTransport {
    fn call(&self, addr: (String, u16), out: Outbound, _timeout_ms: u64) -> Result<OutboundResult, ApiError> {
        let c = FakeCall { addr, method: out.method.clone(), path: out.path.clone(), body: out.body.clone() };
        self.calls.lock().unwrap().push(c.clone());
        (self.responder)(&c)
    }
}

/// Build a canned `(status, body)` outbound result.
pub fn ok(status: u16, body: Value) -> OutboundResult {
    OutboundResult { status, body }
}

/// A **real** S12 `POST /verify` 2xx success body: a FLAT claims object with
/// `valid:true` and a `scope` array (matches `auth-service`
/// `VerifiedClaims::to_json` — there is no nested `claims` wrapper).
pub fn s12_verify_ok(sub: &str, jti: &str, kid: &str, scopes: &[&str], exp_ns: i64) -> Value {
    let iat = exp_ns.saturating_sub(60_000_000_000);
    let scope: Vec<Value> = scopes.iter().map(|s| Value::String((*s).to_string())).collect();
    Value::object(vec![
        ("iss", "auth-service".into()), ("sub", sub.into()), ("jti", jti.into()),
        ("iat", iat.into()), ("nbf", iat.into()), ("exp", exp_ns.into()),
        ("scope", Value::Array(scope)), ("kid", kid.into()), ("valid", true.into()),
    ])
}

/// A S12 `POST /verify` 4xx rejection envelope. `reason` is optional — real
/// S12 populates a few context fields (exp/now, …) but not always a `reason`.
pub fn s12_reject(code: &str, reason: Option<&str>) -> Value {
    let mut ctx: Vec<(&str, Value)> = Vec::new();
    if let Some(r) = reason { ctx.push(("reason", r.into())); }
    let err = Value::object(vec![
        ("code", code.into()), ("message", "auth-service rejected the token.".into()),
        ("service", "auth-service".into()), ("retryable", false.into()),
        ("context", Value::object(ctx)),
    ]);
    Value::object(vec![("error", err)])
}

/// A canned upstream (S9/S14) error envelope for pass-through assertions.
pub fn upstream_error(service: &str, code: &str) -> Value {
    let err = Value::object(vec![
        ("code", code.into()), ("message", "upstream error.".into()),
        ("service", service.into()), ("retryable", false.into()),
        ("context", Value::object(vec![("fill_id", "F1".into())])),
    ]);
    Value::object(vec![("error", err)])
}

/// Default test config (distinct upstream ports so calls are attributable).
pub fn test_config() -> GatewayConfig { GatewayConfig::default() }

/// Build a gateway over a fake transport for handle-level tests.
pub fn gateway(fake: &Arc<FakeTransport>) -> apigw::core::Gateway {
    let t: Arc<dyn Transport> = fake.clone();
    apigw::core::Gateway::new(test_config(), t)
}

/// §1.2 lesson (S13): assert BOTH the HTTP status and the exact error envelope.
pub fn assert_api_envelope(status: u16, body: &Value, expected_status: u16, expected_code: &str, expected_retryable: bool) {
    assert_eq!(status, expected_status, "HTTP status mismatch (got {}, want {})", status, expected_status);
    let err = body.get("error").expect("error body must carry an `error` object");
    assert_eq!(err.get("code").and_then(|v| v.as_str()), Some(expected_code), "envelope code mismatch");
    assert_eq!(err.get("service").and_then(|v| v.as_str()), Some("api-gateway"), "envelope service mismatch");
    assert_eq!(err.get("retryable").and_then(|v| v.as_bool()), Some(expected_retryable), "envelope retryable mismatch");
    let _ = err.get("message").and_then(|m| m.as_str()).expect("envelope must have a message");
}

/// Read a `context.<key>` string field out of an error envelope.
pub fn ctx(body: &Value, key: &str) -> Option<String> {
    body.get("error").and_then(|e| e.get("context")).and_then(|c| c.get(key)).and_then(|v| v.as_str()).map(|s| s.to_string())
}

/// A top-level (non-error) JSON field as `&str`, if present.
pub fn field_str(body: &Value, key: &str) -> Option<String> {
    body.get(key).and_then(|v| v.as_str()).map(|s| s.to_string())
}

/// Shared socket round-trip. Sends `method path` with an optional `Authorization`
/// and an optional raw body, reads the whole response, and returns
/// `(status, parsed-body)`. `raw_body` is sent verbatim (may be invalid JSON).
fn send_recv(
    host: &str, port: u16, method: &str, path: &str,
    raw_body: Option<&str>, bearer: Option<&str>, timeout_ms: u64,
) -> std::io::Result<(u16, Value)> {
    use apigw::json;
    let addr = format!("{}:{}", host, port)
        .to_socket_addrs()
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e.to_string()))?
        .next()
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidInput, "no socket addr"))?;
    let timeout = Duration::from_millis(timeout_ms);
    let mut stream = TcpStream::connect_timeout(&addr, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;

    let payload = raw_body.unwrap_or("").as_bytes().to_vec();
    let mut req = format!("{} {} HTTP/1.1\r\nHost: {}:{}\r\nConnection: close\r\n", method, path, host, port);
    if let Some(tok) = bearer { req.push_str(&format!("Authorization: Bearer {}\r\n", tok)); }
    if raw_body.is_some() {
        req.push_str(&format!("Content-Type: application/json\r\nContent-Length: {}\r\n", payload.len()));
    }
    req.push_str("\r\n");
    stream.write_all(req.as_bytes())?;
    stream.write_all(&payload)?;
    stream.flush()?;

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw)?;
    let head_end = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "no header terminator"))?;
    let head = String::from_utf8_lossy(&raw[..head_end]).into_owned();
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "bad status line"))?;
    let body_text = String::from_utf8_lossy(&raw[head_end + 4..]);
    let parsed = json::parse(body_text.trim()).unwrap_or(Value::Null);
    Ok((status, parsed))
}

/// JSON-body convenience client with an optional `Authorization: Bearer …`.
/// The template `server::request` has no header parameter; authed proxy
/// round-trips are the core of the gateway, so the tests use this instead.
pub fn raw_request(host: &str, port: u16, method: &str, path: &str, body: Option<&Value>, bearer: Option<&str>, timeout_ms: u64) -> std::io::Result<(u16, Value)> {
    let raw = body.map(|b| b.to_json());
    send_recv(host, port, method, path, raw.as_deref(), bearer, timeout_ms)
}

/// Convenience: no bearer, no body.
pub fn raw_get(host: &str, port: u16, path: &str) -> std::io::Result<(u16, Value)> {
    raw_request(host, port, "GET", path, None, None, 2000)
}

/// Send a RAW (unparsed) body — for invalid-JSON tests where the payload is
/// not valid JSON at all.
pub fn raw_request_raw(host: &str, port: u16, method: &str, path: &str, raw_body: &str, bearer: Option<&str>, timeout_ms: u64) -> std::io::Result<(u16, Value)> {
    send_recv(host, port, method, path, Some(raw_body), bearer, timeout_ms)
}
