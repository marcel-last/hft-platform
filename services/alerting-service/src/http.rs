//! alerting_service — std-only HTTP server + request handlers.
//!
//! The server is a small threaded `TcpListener` loop that parses one request
//! per connection (request/response; no keep-alive), reads the body when a
//! `Content-Length` is present, and dispatches through the router.  Handlers
//! receive a `RequestCtx` (including the HTTP verb) and return a `Response`;
//! errors serialize to the standard envelope.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;

use crate::core::SharedManager;
use crate::json::Value;
use crate::models::{self, AlertState, Severity};
use crate::router::{RequestCtx, Response, Router};

/// A route handler: takes a request context, returns a response.
pub type Handler = fn(RequestCtx<'_>) -> Response;

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

fn ok_json(value: Value) -> Response {
    Response::json(200, &value)
}

/// Serialize a `LogicalAlert` to the wire shape (ns fields only — no floats).
fn alert_to_json(a: &crate::core::LogicalAlert) -> Value {
    a.to_json()
}

pub fn h_healthz(_req: RequestCtx<'_>) -> Response {
    ok_json(Value::Object(vec![
        ("status".into(), Value::String("ok".into())),
        ("service".into(), Value::String("alerting-service".into())),
        ("version".into(), Value::String("1.0.0".into())),
    ]))
}

pub fn h_readyz(req: RequestCtx<'_>) -> Response {
    let active = req.manager.active_count();
    ok_json(Value::Object(vec![
        ("status".into(), Value::String("ready".into())),
        ("reasons".into(), Value::Array(Vec::new())),
        ("active_alerts".into(), Value::Int(active as i64)),
        ("tracked_alerts".into(), Value::Int(req.manager.alert_count() as i64)),
    ]))
}

/// POST /alerts — submit an alert occurrence.  The response reports how the
/// occurrence was treated (new vs suppressed) plus the logical alert id.
pub fn h_alerts(req: RequestCtx<'_>) -> Response {
    if req.method != "POST" || req.body.trim().is_empty() {
        return Response::error(&crate::errors::Error::bad_request(
            "POST /alerts requires a JSON body",
        ));
    }
    let body = match models::parse_json(req.body) {
        Ok(v) => v,
        Err(e) => return Response::error(&e),
    };
    if !body.is_object() {
        return Response::error(&crate::errors::Error::bad_request(
            "request body must be a JSON object",
        ));
    }
    let now = models::now_ns();
    let event = match models::AlertEvent::parse(&body, now) {
        Ok(e) => e,
        Err(e) => return Response::error(&e),
    };
    match req.manager.submit(&event) {
        Ok(outcome) => ok_json(Value::Object(vec![
            ("outcome".into(), outcome.to_json()),
            (
                "alert".into(),
                req.manager
                    .get(&outcome.alert_id)
                    .map(|a| alert_to_json(&a))
                    .unwrap_or(Value::Null),
            ),
        ])),
        Err(e) => Response::error(&e),
    }
}

/// GET /alerts — list logical alerts.  Optional query filters: `source`,
/// `state` (ACTIVE/ACKNOWLEDGED/RESOLVED), `severity` (INFO/WARNING/CRITICAL),
/// `limit`.
pub fn h_alerts_list(req: RequestCtx<'_>) -> Response {
    let source = req.query.get("source").map(|s| s.as_str());
    let state = req
        .query
        .get("state")
        .and_then(|s| match s.to_ascii_uppercase().as_str() {
            "ACTIVE" => Some(AlertState::Active),
            "ACKNOWLEDGED" => Some(AlertState::Acknowledged),
            "RESOLVED" => Some(AlertState::Resolved),
            _ => None,
        });
    let severity = req
        .query
        .get("severity")
        .and_then(|s| Severity::from_str(s));
    let limit = req
        .query
        .get("limit")
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(0);

    let alerts = req.manager.list(source, state, severity, limit);
    ok_json(Value::Object(vec![
        ("count".into(), Value::Int(alerts.len() as i64)),
        (
            "alerts".into(),
            Value::Array(alerts.iter().map(alert_to_json).collect()),
        ),
    ]))
}

/// GET /alerts/{id} — a single logical alert.  404 ALT-202 when unknown.
pub fn h_alert(req: RequestCtx<'_>) -> Response {
    let id = req.params.get("id").cloned().unwrap_or_default();
    match req.manager.get(&id) {
        Some(a) => ok_json(alert_to_json(&a)),
        None => Response::error(&crate::errors::Error::unknown_alert(&id)),
    }
}

/// POST /alerts/ack — acknowledge an alert.  Body: `{"alert_id": "ALT-00000001"}`.
pub fn h_ack(req: RequestCtx<'_>) -> Response {
    if req.method != "POST" || req.body.trim().is_empty() {
        return Response::error(&crate::errors::Error::bad_request(
            "POST /alerts/ack requires a JSON body with 'alert_id'",
        ));
    }
    let body = match models::parse_json(req.body) {
        Ok(v) => v,
        Err(e) => return Response::error(&e),
    };
    let alert_id = match body.get("alert_id").and_then(|v| v.as_str()) {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => {
            return Response::error(&crate::errors::Error::bad_request(
                "missing or empty 'alert_id' field",
            ))
        }
    };
    match req.manager.acknowledge(&alert_id) {
        Ok(ticket) => ok_json(Value::Object(vec![
            ("ticket".into(), ticket.to_json()),
            (
                "alert".into(),
                req.manager
                    .get(&alert_id)
                    .map(|a| alert_to_json(&a))
                    .unwrap_or(Value::Null),
            ),
        ])),
        Err(e) => Response::error(&e),
    }
}

/// POST /alerts/{id}/resolve — resolve (close) an alert.  Idempotent.
pub fn h_resolve(req: RequestCtx<'_>) -> Response {
    let id = req.params.get("id").cloned().unwrap_or_default();
    match req.manager.resolve(&id) {
        Ok(state) => ok_json(Value::Object(vec![
            ("alert_id".into(), Value::String(id)),
            (
                "state".into(),
                Value::String(state.as_str().to_string()),
            ),
        ])),
        Err(e) => Response::error(&e),
    }
}

/// GET /alerts/dispatch — the bounded dispatch log, newest first.  `limit`
/// bounds the result.
pub fn h_alerts_dispatch(req: RequestCtx<'_>) -> Response {
    let limit = req
        .query
        .get("limit")
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(0);
    let log = req.manager.dispatch_log(limit);
    ok_json(Value::Object(vec![
        ("count".into(), Value::Int(log.len() as i64)),
        ("dispatches".into(), Value::Array(log)),
    ]))
}

/// GET /stats — aggregate counters.
pub fn h_stats(req: RequestCtx<'_>) -> Response {
    let s = req.manager.stats();
    ok_json(Value::Object(vec![
        (
            "received_total".into(),
            Value::Int(s.received_total as i64),
        ),
        ("new_alerts_total".into(), Value::Int(s.new_alerts_total as i64)),
        (
            "suppressed_total".into(),
            Value::Int(s.suppressed_total as i64),
        ),
        (
            "escalated_total".into(),
            Value::Int(s.escalated_total as i64),
        ),
        ("oncall_total".into(), Value::Int(s.oncall_total as i64)),
        ("acks_total".into(), Value::Int(s.acks_total as i64)),
        ("tracked_alerts".into(), Value::Int(req.manager.alert_count() as i64)),
        (
            "active_alerts".into(),
            Value::Int(req.manager.active_count() as i64),
        ),
    ]))
}

// ---------------------------------------------------------------------------
// Request parsing
// ---------------------------------------------------------------------------

/// Parse the request line + headers into (method, path, query, content_length).
fn parse_request_head(buf: &str) -> Option<(String, String, HashMap<String, String>, usize)> {
    let head_end = buf.find("\r\n\r\n")?;
    let head = &buf[..head_end];
    let mut lines = head.split("\r\n");
    let request_line = lines.next()?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next()?.to_string();
    let target = parts.next()?.to_string();

    let (path, query_str) = match target.find('?') {
        Some(i) => (&target[..i], &target[i + 1..]),
        None => (target.as_str(), ""),
    };

    let mut content_length = 0usize;
    for line in lines {
        if line.is_empty() {
            continue;
        }
        if let Some((k, v)) = line.split_once(':') {
            if k.trim().eq_ignore_ascii_case("content-length") {
                content_length = v.trim().parse().unwrap_or(0);
            }
        }
    }

    let mut query = HashMap::new();
    for pair in query_str.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (k, v) = match pair.find('=') {
            Some(i) => (&pair[..i], &pair[i + 1..]),
            None => (pair, ""),
        };
        query.insert(url_decode(k), url_decode(v));
    }

    Some((method, path.to_string(), query, content_length))
}

fn url_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'+' {
            out.push(b' ');
            i += 1;
        } else if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("00");
            match u8::from_str_radix(hex, 16) {
                Ok(b) => {
                    out.push(b);
                    i += 3;
                }
                Err(_) => {
                    out.push(bytes[i]);
                    i += 1;
                }
            }
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn head_len(buf: &[u8]) -> Option<usize> {
    buf.windows(4)
        .position(|w| w == b"\r\n\r\n")
        .map(|i| i + 4)
}

// ---------------------------------------------------------------------------
// Server loop
// ---------------------------------------------------------------------------

/// Serve one connection: read the full request, dispatch, write the response.
fn handle_connection(mut stream: TcpStream, router: &Router, manager: &SharedManager) {
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(5)));
    let mut buf: Vec<u8> = Vec::with_capacity(4096);
    let mut chunk = [0u8; 4096];

    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                // stop once we have the head plus the full body
                let need_more = match parse_request_head(&String::from_utf8_lossy(&buf)) {
                    Some((_, _, _, content_length)) => match head_len(&buf) {
                        Some(h) => h + content_length > buf.len(),
                        None => true,
                    },
                    None => true,
                };
                if !need_more {
                    break;
                }
            }
            Err(_) => return,
        }
    }

    let raw = String::from_utf8_lossy(&buf).into_owned();
    let (method, path, query, content_length) = match parse_request_head(&raw) {
        Some(t) => t,
        None => return,
    };
    // The body begins immediately after the head's terminating "\r\n\r\n".
    let body = if content_length > 0 {
        let hlen = head_len(&buf).unwrap_or(0);
        let start = hlen;
        let end = (start + content_length).min(buf.len());
        String::from_utf8_lossy(&buf[start..end]).into_owned()
    } else {
        String::new()
    };

    let response = router.dispatch(&method, &path, query, &body, manager);
    write_response(&mut stream, &response);
}

fn write_response(stream: &mut TcpStream, resp: &Response) {
    let reason = match resp.status {
        200 => "OK",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        _ => "OK",
    };
    let head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        resp.status,
        reason,
        resp.body.len()
    );
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(&resp.body);
    let _ = stream.flush();
}

/// Start the HTTP server on `port` in a background thread.  Returns the bound
/// port (useful when `0` is passed for an ephemeral port in tests).
pub fn serve(port: u16, router: Router, manager: SharedManager) -> std::io::Result<u16> {
    let listener = TcpListener::bind(("0.0.0.0", port))?;
    let bound_port = listener.local_addr()?.port();
    thread::Builder::new()
        .name("altsvc-http".into())
        .spawn(move || {
            for stream in listener.incoming() {
                if let Ok(s) = stream {
                    // Clone the router per-connection (cheap: a Vec of fn pointers).
                    let r = Router::clone(&router);
                    let m = std::sync::Arc::clone(&manager);
                    thread::spawn(move || handle_connection(s, &r, &m));
                }
            }
        })?;
    Ok(bound_port)
}
