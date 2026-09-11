//! std-only threaded HTTP/1.1 server: accept loop, request parsing, response
//! writer, standard error envelope, and a minimal client for tests and
//! inter-service calls.
//!
//! TEMPLATE FILE — copied verbatim into every Rust service from
//! `templates/rust/`. Do not edit inside a service; fix here and re-copy.
//!
//! Requires `crate::json` and `crate::router` from the same template set.
//! The service's `http.rs` supplies the handler closure (routing + handlers);
//! this file never references service types.

use crate::json::{self, JsonError, Value};
use crate::router::split_target;
use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream, ToSocketAddrs};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

pub const MAX_HEADER_BYTES: usize = 16 * 1024;

#[derive(Debug, Clone)]
pub struct Request {
    /// Upper-cased method, e.g. `"POST"`.
    pub method: String,
    /// Path without query string, e.g. `"/keys/key-01"`.
    pub path: String,
    /// Decoded query pairs in order of appearance.
    pub query: Vec<(String, String)>,
    /// Header names lower-cased, values trimmed.
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Request {
    pub fn header(&self, name: &str) -> Option<&str> {
        let name = name.to_ascii_lowercase();
        self.headers.iter().find(|(k, _)| *k == name).map(|(_, v)| v.as_str())
    }

    pub fn query_param(&self, name: &str) -> Option<&str> {
        self.query.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }

    /// Parse the body as JSON. An empty body parses as `{}`.
    pub fn json(&self) -> Result<Value, JsonError> {
        if self.body.iter().all(u8::is_ascii_whitespace) {
            return Ok(Value::Object(Vec::new()));
        }
        json::parse(&String::from_utf8_lossy(&self.body))
    }
}

#[derive(Debug, Clone)]
pub struct Response {
    pub status: u16,
    pub body: Value,
}

impl Response {
    pub fn json(status: u16, body: Value) -> Self {
        Self { status, body }
    }

    pub fn ok(body: Value) -> Self {
        Self { status: 200, body }
    }

    /// Standard error envelope (CONVENTIONS.md §1.2).
    pub fn error(
        status: u16,
        code: &str,
        message: &str,
        service: &str,
        retryable: bool,
        context: Value,
    ) -> Self {
        let context = match context {
            Value::Object(_) => context,
            _ => Value::Object(Vec::new()),
        };
        let inner = Value::object(vec![
            ("code", code.into()),
            ("message", message.into()),
            ("service", service.into()),
            ("retryable", retryable.into()),
            ("context", context),
        ]);
        Self { status, body: Value::object(vec![("error", inner)]) }
    }
}

#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// Service directory name, used in protocol-level error envelopes.
    pub service: String,
    /// Error code for protocol-level rejects (bad request line, oversized body).
    pub protocol_error_code: String,
    pub max_body_bytes: usize,
    pub read_timeout_ms: u64,
}

impl ServerConfig {
    pub fn new(service: &str, protocol_error_code: &str) -> Self {
        Self {
            service: service.to_string(),
            protocol_error_code: protocol_error_code.to_string(),
            max_body_bytes: 1024 * 1024,
            read_timeout_ms: 5_000,
        }
    }
}

pub type Handler = Arc<dyn Fn(&Request) -> Response + Send + Sync>;

pub struct ServerHandle {
    port: u16,
    thread: JoinHandle<()>,
}

impl ServerHandle {
    pub fn port(&self) -> u16 {
        self.port
    }

    /// Block the calling thread for the lifetime of the server (used by `main`).
    pub fn join(self) {
        let _ = self.thread.join();
    }
}

/// Bind `bind:port` (port `0` = ephemeral, read it back via `ServerHandle::port`)
/// and serve on a background thread, one thread per connection.
pub fn serve(bind: &str, port: u16, cfg: ServerConfig, handler: Handler) -> io::Result<ServerHandle> {
    let listener = TcpListener::bind((bind, port))?;
    let port = listener.local_addr()?.port();
    let cfg = Arc::new(cfg);
    let thread = thread::spawn(move || {
        for conn in listener.incoming() {
            let stream = match conn {
                Ok(s) => s,
                Err(_) => continue,
            };
            let handler = Arc::clone(&handler);
            let cfg = Arc::clone(&cfg);
            thread::spawn(move || handle_connection(stream, &cfg, &handler));
        }
    });
    Ok(ServerHandle { port, thread })
}

fn handle_connection(mut stream: TcpStream, cfg: &ServerConfig, handler: &Handler) {
    let _ = stream.set_read_timeout(Some(Duration::from_millis(cfg.read_timeout_ms)));
    let _ = stream.set_nodelay(true);
    let response = match read_request(&mut stream, cfg) {
        Ok(req) => handler(&req),
        Err(e) => e.into_response(cfg),
    };
    let _ = write_response(&mut stream, &response);
    let _ = stream.shutdown(Shutdown::Both);
}

enum ProtocolError {
    BadRequest(&'static str),
    TooLarge(usize),
    Io(io::Error),
}

impl ProtocolError {
    fn into_response(self, cfg: &ServerConfig) -> Response {
        let (status, detail) = match self {
            ProtocolError::BadRequest(d) => (400, d.to_string()),
            ProtocolError::TooLarge(n) => (413, format!("body of {} bytes exceeds limit", n)),
            ProtocolError::Io(e) => (400, format!("read failed: {}", e)),
        };
        Response::error(
            status,
            &cfg.protocol_error_code,
            "Malformed HTTP request.",
            &cfg.service,
            false,
            Value::object(vec![("detail", detail.into())]),
        )
    }
}

fn find_header_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n")
}

fn read_request(stream: &mut TcpStream, cfg: &ServerConfig) -> Result<Request, ProtocolError> {
    let mut buf: Vec<u8> = Vec::with_capacity(1024);
    let mut chunk = [0u8; 4096];
    let head_end = loop {
        if let Some(i) = find_header_end(&buf) {
            break i;
        }
        if buf.len() > MAX_HEADER_BYTES {
            return Err(ProtocolError::BadRequest("headers too large"));
        }
        let n = stream.read(&mut chunk).map_err(ProtocolError::Io)?;
        if n == 0 {
            return Err(ProtocolError::BadRequest("connection closed before headers completed"));
        }
        buf.extend_from_slice(&chunk[..n]);
    };

    let head = std::str::from_utf8(&buf[..head_end])
        .map_err(|_| ProtocolError::BadRequest("non-UTF-8 request head"))?;
    let mut lines = head.split("\r\n");
    let mut parts = lines.next().unwrap_or("").split_whitespace();
    let method = parts
        .next()
        .ok_or(ProtocolError::BadRequest("missing method"))?
        .to_ascii_uppercase();
    let target = parts.next().ok_or(ProtocolError::BadRequest("missing request target"))?;
    if !parts.next().unwrap_or("").starts_with("HTTP/1.") {
        return Err(ProtocolError::BadRequest("unsupported HTTP version"));
    }

    let mut headers = Vec::new();
    for line in lines.filter(|l| !l.is_empty()) {
        let i = line.find(':').ok_or(ProtocolError::BadRequest("malformed header line"))?;
        headers.push((line[..i].trim().to_ascii_lowercase(), line[i + 1..].trim().to_string()));
    }
    if headers
        .iter()
        .any(|(k, v)| k == "transfer-encoding" && v.to_ascii_lowercase().contains("chunked"))
    {
        return Err(ProtocolError::BadRequest("chunked transfer encoding is not supported"));
    }
    let content_length = match headers.iter().find(|(k, _)| k == "content-length") {
        Some((_, v)) => v
            .parse::<usize>()
            .map_err(|_| ProtocolError::BadRequest("invalid Content-Length"))?,
        None => 0,
    };
    if content_length > cfg.max_body_bytes {
        return Err(ProtocolError::TooLarge(content_length));
    }

    // Body starts right after the blank line; never derive it from buf.len().
    let mut body: Vec<u8> = buf[head_end + 4..].to_vec();
    while body.len() < content_length {
        let n = stream.read(&mut chunk).map_err(ProtocolError::Io)?;
        if n == 0 {
            return Err(ProtocolError::BadRequest("connection closed before body completed"));
        }
        body.extend_from_slice(&chunk[..n]);
    }
    body.truncate(content_length);

    let (path, query) = split_target(target);
    Ok(Request { method, path: path.to_string(), query, headers, body })
}

pub fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        204 => "No Content",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        413 => "Payload Too Large",
        422 => "Unprocessable Entity",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        503 => "Service Unavailable",
        _ => "Unknown",
    }
}

fn write_response(stream: &mut TcpStream, resp: &Response) -> io::Result<()> {
    let body = resp.body.to_json();
    let head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        resp.status,
        reason_phrase(resp.status),
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body.as_bytes())?;
    stream.flush()
}

// ---------------------------------------------------------------------------
// Minimal client: integration tests and outbound inter-service calls.
// ---------------------------------------------------------------------------

/// Send one request and return `(status, parsed JSON body)`. A non-JSON or
/// empty response body yields `Value::Null`. Uses `Connection: close`.
pub fn request<A: ToSocketAddrs>(
    addr: A,
    method: &str,
    path: &str,
    body: Option<&Value>,
    timeout_ms: u64,
) -> io::Result<(u16, Value)> {
    let addr = addr
        .to_socket_addrs()?
        .next()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "unresolvable address"))?;
    let timeout = Duration::from_millis(timeout_ms);
    let mut stream = TcpStream::connect_timeout(&addr, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;

    let payload = body.map(|b| b.to_json()).unwrap_or_default();
    let mut req = format!("{} {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n", method, path, addr);
    if body.is_some() {
        req.push_str(&format!(
            "Content-Type: application/json\r\nContent-Length: {}\r\n",
            payload.len()
        ));
    }
    req.push_str("\r\n");
    stream.write_all(req.as_bytes())?;
    stream.write_all(payload.as_bytes())?;
    stream.flush()?;

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw)?;
    let head_end = find_header_end(&raw)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "no header terminator in response"))?;
    let head = String::from_utf8_lossy(&raw[..head_end]).into_owned();
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "bad status line"))?;
    let body_text = String::from_utf8_lossy(&raw[head_end + 4..]);
    let parsed = json::parse(body_text.trim()).unwrap_or(Value::Null);
    Ok((status, parsed))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn start() -> ServerHandle {
        let cfg = ServerConfig { max_body_bytes: 64, ..ServerConfig::new("test-svc", "TST-001") };
        let handler: Handler = Arc::new(|req: &Request| match (req.method.as_str(), req.path.as_str()) {
            ("GET", "/echo") => Response::ok(Value::object(vec![
                ("q", req.query_param("x").unwrap_or("").into()),
                ("agent", req.header("X-Agent").unwrap_or("").into()),
            ])),
            ("POST", "/echo") => match req.json() {
                Ok(v) => Response::json(201, v),
                Err(e) => Response::error(400, "TST-002", &e.to_string(), "test-svc", false, Value::Null),
            },
            _ => Response::error(404, "TST-404", "No such route.", "test-svc", false, Value::Null),
        });
        serve("127.0.0.1", 0, cfg, handler).unwrap()
    }

    #[test]
    fn get_with_query_and_headers() {
        let h = start();
        let mut s = TcpStream::connect(("127.0.0.1", h.port())).unwrap();
        s.write_all(b"GET /echo?x=a%20b HTTP/1.1\r\nHost: x\r\nX-Agent: t1\r\nConnection: close\r\n\r\n")
            .unwrap();
        let mut raw = Vec::new();
        s.read_to_end(&mut raw).unwrap();
        let text = String::from_utf8_lossy(&raw);
        assert!(text.starts_with("HTTP/1.1 200 OK\r\n"));
        assert!(text.contains("Content-Type: application/json"));
        let body = &text[text.find("\r\n\r\n").unwrap() + 4..];
        assert_eq!(body, r#"{"q":"a b","agent":"t1"}"#);
    }

    #[test]
    fn post_roundtrip_via_client() {
        let h = start();
        let body = Value::object(vec![("k", 1i64.into()), ("s", "v".into())]);
        let (status, resp) = request(("127.0.0.1", h.port()), "POST", "/echo", Some(&body), 2_000).unwrap();
        assert_eq!(status, 201);
        assert_eq!(resp, body);
        let (status, resp) = request(("127.0.0.1", h.port()), "GET", "/missing", None, 2_000).unwrap();
        assert_eq!(status, 404);
        assert_eq!(resp.get("error").and_then(|e| e.get("code")).and_then(Value::as_str), Some("TST-404"));
    }

    #[test]
    fn protocol_errors() {
        let h = start();
        let (status, resp) = request(("127.0.0.1", h.port()), "POST", "/echo", Some(&Value::from("x".repeat(100))), 2_000).unwrap();
        assert_eq!(status, 413);
        assert_eq!(resp.get("error").and_then(|e| e.get("code")).and_then(Value::as_str), Some("TST-001"));

        let mut s = TcpStream::connect(("127.0.0.1", h.port())).unwrap();
        s.write_all(b"NOT A REQUEST\r\n\r\n").unwrap();
        let mut raw = Vec::new();
        s.read_to_end(&mut raw).unwrap();
        assert!(String::from_utf8_lossy(&raw).starts_with("HTTP/1.1 400 Bad Request"));
    }
}