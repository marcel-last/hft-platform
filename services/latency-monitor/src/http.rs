//! latency_monitor — std-only HTTP server + request handlers.
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

use crate::core::SharedMonitor;
use crate::json::Value;
use crate::models::{self, Stage};
use crate::router::{RequestCtx, Response, Router};

/// A route handler: takes a request context, returns a response.
pub type Handler = fn(RequestCtx<'_>) -> Response;

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

fn ok_json(value: Value) -> Response {
    Response::json(200, &value)
}

/// Serialize a `StageStats` to the wire shape (ns fields only — no floats).
fn stats_to_json(s: &crate::core::StageStats) -> Value {
    Value::Object(vec![
        ("stage".into(), Value::String(s.stage.as_str().to_string())),
        ("count".into(), Value::Int(s.count as i64)),
        ("total".into(), Value::Int(s.total as i64)),
        (
            "min_ns".into(),
            s.min_ns.map(Value::Int).unwrap_or(Value::Null),
        ),
        (
            "max_ns".into(),
            s.max_ns.map(Value::Int).unwrap_or(Value::Null),
        ),
        (
            "mean_ns".into(),
            s.mean_ns.map(Value::Int).unwrap_or(Value::Null),
        ),
        (
            "p50_ns".into(),
            s.p50_ns.map(Value::Int).unwrap_or(Value::Null),
        ),
        (
            "p99_ns".into(),
            s.p99_ns.map(Value::Int).unwrap_or(Value::Null),
        ),
        (
            "p999_ns".into(),
            s.p999_ns.map(Value::Int).unwrap_or(Value::Null),
        ),
        (
            "budget_ns".into(),
            s.budget_ns.map(Value::Int).unwrap_or(Value::Null),
        ),
        ("breached".into(), Value::Bool(s.breached)),
    ])
}

pub fn h_healthz(_req: RequestCtx<'_>) -> Response {
    ok_json(Value::Object(vec![
        ("status".into(), Value::String("ok".into())),
        ("service".into(), Value::String("latency-monitor".into())),
        ("version".into(), Value::String("1.0.0".into())),
    ]))
}

pub fn h_readyz(req: RequestCtx<'_>) -> Response {
    let total = req.monitor.total_samples();
    ok_json(Value::Object(vec![
        ("status".into(), Value::String("ready".into())),
        ("reasons".into(), Value::Array(Vec::new())),
        ("total_samples".into(), Value::Int(total as i64)),
        ("alerts_pending".into(), Value::Int(req.monitor.alert_count() as i64)),
    ]))
}

/// POST /stamp — record a latency sample.  The response echoes the recorded
/// sample plus any alert that was emitted (or `null`).
pub fn h_stamp(req: RequestCtx<'_>) -> Response {
    if req.method != "POST" || req.body.trim().is_empty() {
        return Response::error(&crate::errors::Error::bad_request(
            "POST /stamp requires a JSON body",
        ));
    }
    let body = match models::parse_json(req.body) {
        Ok(v) => v,
        Err(e) => return Response::error(&e),
    };
    let now = models::now_ns();
    let sample = match crate::models::LatencySample::parse(&body, now) {
        Ok(s) => s,
        Err(e) => return Response::error(&e),
    };
    match req.monitor.record(&sample) {
        Ok(alert) => ok_json(Value::Object(vec![
            ("sample".into(), sample.to_json()),
            (
                "alert".into(),
                alert.map(|a| a.to_json()).unwrap_or(Value::Null),
            ),
        ])),
        Err(e) => Response::error(&e),
    }
}

/// GET /latency — statistics for every stage.  Optional `?stage=` filter is
/// handled by the dedicated route; this returns the full table.
pub fn h_latency(req: RequestCtx<'_>) -> Response {
    let stats = req.monitor.all_stats();
    ok_json(Value::Object(vec![
        ("count".into(), Value::Int(stats.len() as i64)),
        (
            "stages".into(),
            Value::Array(stats.iter().map(stats_to_json).collect()),
        ),
        ("total_samples".into(), Value::Int(req.monitor.total_samples() as i64)),
    ]))
}

/// GET /latency/{stage} — statistics for one stage.  Query `?min=false` relaxes
/// the minimum-sample requirement (default: enforce it, returning 409 LAT-204).
pub fn h_latency_stage(req: RequestCtx<'_>) -> Response {
    let name = req.params.get("stage").cloned().unwrap_or_default();
    let stage = match Stage::from_str(&name) {
        Some(s) => s,
        None => return Response::error(&crate::errors::Error::unknown_stage(&name)),
    };
    let require_min = req
        .query
        .get("min")
        .map(|v| v != "false" && v != "0")
        .unwrap_or(true);
    match req.monitor.stage_stats(stage, require_min) {
        Ok(s) => ok_json(stats_to_json(&s)),
        Err(e) => Response::error(&e),
    }
}

/// GET /percentiles — a compact p50/p99/p999 table for all stages (ns).
pub fn h_percentiles(_req: RequestCtx<'_>) -> Response {
    let rows = crate::models::Stage::all()
        .iter()
        .map(|&s| {
            Value::Object(vec![
                ("stage".into(), Value::String(s.as_str().to_string())),
                (
                    "p50_ns".into(),
                    _req
                        .monitor
                        .percentile(s, 50.0)
                        .map(Value::Int)
                        .unwrap_or(Value::Null),
                ),
                (
                    "p99_ns".into(),
                    _req
                        .monitor
                        .percentile(s, 99.0)
                        .map(Value::Int)
                        .unwrap_or(Value::Null),
                ),
                (
                    "p999_ns".into(),
                    _req
                        .monitor
                        .percentile(s, 99.9)
                        .map(Value::Int)
                        .unwrap_or(Value::Null),
                ),
            ])
        })
        .collect::<Vec<Value>>();
    ok_json(Value::Object(vec![
        ("count".into(), Value::Int(rows.len() as i64)),
        ("percentiles".into(), Value::Array(rows)),
    ]))
}

/// GET /budgets — the configured per-stage budgets plus breach state.
pub fn h_budgets(req: RequestCtx<'_>) -> Response {
    let rows = req
        .monitor
        .budgets()
        .iter()
        .map(|b| {
            let stats = req.monitor.stage_stats(b.stage, false);
            let breached = stats.as_ref().map(|s| s.breached).unwrap_or(false);
            let mut pairs = vec![
                ("stage".into(), Value::String(b.stage.as_str().to_string())),
                ("budget_ns".into(), Value::Int(b.budget_ns)),
                ("breached".into(), Value::Bool(breached)),
            ];
            if let Ok(s) = &stats {
                pairs.push((
                    "p99_ns".into(),
                    s.p99_ns.map(Value::Int).unwrap_or(Value::Null),
                ));
            }
            Value::Object(pairs)
        })
        .collect::<Vec<Value>>();
    ok_json(Value::Object(vec![
        ("count".into(), Value::Int(rows.len() as i64)),
        ("budgets".into(), Value::Array(rows)),
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
fn handle_connection(mut stream: TcpStream, router: &Router, monitor: &SharedMonitor) {
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

    let response = router.dispatch(&method, &path, query, &body, monitor);
    write_response(&mut stream, &response);
}

fn write_response(stream: &mut TcpStream, resp: &Response) {
    let reason = match resp.status {
        200 => "OK",
        400 => "Bad Request",
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
pub fn serve(port: u16, router: Router, monitor: SharedMonitor) -> std::io::Result<u16> {
    let listener = TcpListener::bind(("0.0.0.0", port))?;
    let bound_port = listener.local_addr()?.port();
    thread::Builder::new()
        .name("latmon-http".into())
        .spawn(move || {
            for stream in listener.incoming() {
                if let Ok(s) = stream {
                    // Clone the router per-connection (cheap: a Vec of fn pointers).
                    let r = Router::clone(&router);
                    let m = std::sync::Arc::clone(&monitor);
                    thread::spawn(move || handle_connection(s, &r, &m));
                }
            }
        })?;
    Ok(bound_port)
}
