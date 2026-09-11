//! execution_gateway — std-only HTTP server + request handlers.
//!
//! The server is a small threaded `TcpListener` loop that parses one request
//! per connection (request/response; no keep-alive), reads the body when a
//! `Content-Length` is present, and dispatches through the router.  Handlers
//! receive a :struct:`RequestCtx` (including the HTTP verb) and return a
//! :struct:`Response`; errors serialize to the standard envelope.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;

use crate::core::SharedManager;
use crate::json::Value;
use crate::models;
use crate::router::{RequestCtx, Response, Router};

/// A route handler: takes a request context, returns a response.
pub type Handler = fn(RequestCtx<'_>) -> Response;

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

fn ok_json(value: Value) -> Response {
    Response::json(200, &value)
}

pub fn h_healthz(_req: RequestCtx<'_>) -> Response {
    ok_json(Value::Object(vec![
        ("status".into(), Value::String("ok".into())),
        ("service".into(), Value::String("execution-gateway".into())),
        ("version".into(), Value::String("1.0.0".into())),
    ]))
}

pub fn h_readyz(req: RequestCtx<'_>) -> Response {
    let open = req.manager.open_count();
    ok_json(Value::Object(vec![
        ("status".into(), Value::String("ready".into())),
        ("reasons".into(), Value::Array(Vec::new())),
        ("open_orders".into(), Value::Int(open as i64)),
    ]))
}

/// POST /orders — submit a new order intent.  GET /orders — list orders.
pub fn h_orders(req: RequestCtx<'_>) -> Response {
    if req.method == "POST" && !req.body.trim().is_empty() {
        let body = match models::parse_json(req.body) {
            Ok(v) => v,
            Err(e) => return Response::error(&e),
        };
        let now = models::now_ns();
        let order = match models::parse_intent(&body, "SIM", now) {
            Ok(o) => o,
            Err(e) => return Response::error(&e),
        };
        return match req.manager.submit(order, now) {
            Ok(o) => ok_json(o.to_json()),
            Err(e) => Response::error(&e),
        };
    }
    // GET /orders — list
    let limit = req.query.get("limit").and_then(|s| s.parse::<usize>().ok()).unwrap_or(100);
    let orders = req.manager.list(limit);
    ok_json(Value::Object(vec![
        ("count".into(), Value::Int(orders.len() as i64)),
        ("orders".into(), Value::Array(orders.iter().map(|o| o.to_json()).collect())),
    ]))
}

/// GET / PATCH / DELETE /orders/{id}
pub fn h_order_id(req: RequestCtx<'_>) -> Response {
    let id = req.params.get("id").cloned().unwrap_or_default();
    let now = models::now_ns();
    match req.method.as_str() {
        "PATCH" => {
            let body = match models::parse_json(req.body) {
                Ok(v) => v,
                Err(e) => return Response::error(&e),
            };
            let mod_req = match models::ModifyRequest::parse(&body) {
                Ok(r) => r,
                Err(e) => return Response::error(&e),
            };
            match req.manager.modify(&id, &mod_req, now) {
                Ok(o) => ok_json(o.to_json()),
                Err(e) => Response::error(&e),
            }
        }
        "DELETE" => match req.manager.cancel(&id, now) {
            Ok(o) => ok_json(o.to_json()),
            Err(e) => Response::error(&e),
        },
        _ => match req.manager.get(&id) {
            Ok(o) => ok_json(o.to_json()),
            Err(e) => Response::error(&e),
        },
    }
}

/// GET /fills
pub fn h_fills(req: RequestCtx<'_>) -> Response {
    let limit = req.query.get("limit").and_then(|s| s.parse::<usize>().ok()).unwrap_or(100);
    let fills = req.manager.fills(limit);
    ok_json(Value::Object(vec![
        ("count".into(), Value::Int(fills.len() as i64)),
        ("fills".into(), Value::Array(fills.iter().map(|f| f.to_json()).collect())),
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
        .name("exg-http".into())
        .spawn(move || {
            for stream in listener.incoming() {
                if let Ok(s) = stream {
                    // Clone the router per-connection (cheap: a Vec of closures).
                    let r = Router::clone(&router);
                    let m = std::sync::Arc::clone(&manager);
                    thread::spawn(move || handle_connection(s, &r, &m));
                }
            }
        })?;
    Ok(bound_port)
}
