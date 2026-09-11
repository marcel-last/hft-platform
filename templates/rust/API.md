# templates/rust — public API

Copy with `cp templates/rust/{json,router,server}.rs services/<svc>/src/`
(add `crypto.rs` when the service needs it). Declare in `lib.rs`:
`pub mod json; pub mod router; pub mod server;` (+ `pub mod crypto;`).
Each file carries its own `#[cfg(test)]` tests; they run under `cargo test`.
Never edit a copied template inside a service — fix it in `templates/rust/`.

## json.rs
```rust
pub enum Value { Null, Bool(bool), Int(i64), Float(f64), String(String),
                 Array(Vec<Value>), Object(Vec<(String, Value)>) }   // Debug, Clone, PartialEq
pub struct JsonError { pub message: String, pub offset: usize }      // Display, Error
pub fn parse(input: &str) -> Result<Value, JsonError>                // rejects trailing garbage, depth > 128
impl Value {
    pub fn object(pairs: Vec<(&str, Value)>) -> Value                // Value::object(vec![("k", 1i64.into())])
    pub fn get(&self, key: &str) -> Option<&Value>
    pub fn set(&mut self, key: &str, value: Value)                   // insert or replace; no-op on non-object
    pub fn as_i64(&self) -> Option<i64>       pub fn as_f64(&self) -> Option<f64>
    pub fn as_str(&self) -> Option<&str>      pub fn as_bool(&self) -> Option<bool>
    pub fn as_array(&self) -> Option<&[Value]> pub fn as_object(&self) -> Option<&[(String, Value)]>
    pub fn is_null(&self) -> bool             pub fn to_json(&self) -> String   // also impl Display
}
// From<bool|i32|i64|u64|usize|f64|&str|String|Vec<Value>|Option<T>> for Value
// u64/usize saturate at i64::MAX — use `.into()` for counters, no manual casts.
```

## router.rs
```rust
pub struct Router;                                   // Router::new()
impl Router {
    pub fn add(&mut self, method: &'static str, pattern: &'static str, name: &'static str) -> &mut Self
    pub fn resolve(&self, method: &str, path: &str) -> Resolution      // path WITHOUT query string
    pub fn routes(&self) -> Vec<(&'static str, String, &'static str)>  // (method, pattern, name)
}
pub enum Resolution {
    Found { name: &'static str, params: Params },
    MethodNotAllowed { allowed: Vec<&'static str> },
    NotFound,
}
pub struct Params;  impl Params { pub fn get(&self, name: &str) -> Option<&str> }   // percent-decoded
pub fn split_target(target: &str) -> (&str, Vec<(String, String)>)  // "/a?x=1" -> ("/a", [("x","1")])
pub fn parse_query(query: &str) -> Vec<(String, String)>
pub fn percent_decode(s: &str) -> String
```
Patterns: `/keys/{kid}`, `/orders/{id}/fills`. Trailing slashes and method case are tolerated.

## server.rs
```rust
pub struct Request { pub method: String, pub path: String, pub query: Vec<(String, String)>,
                     pub headers: Vec<(String, String)>, pub body: Vec<u8> }
impl Request {
    pub fn header(&self, name: &str) -> Option<&str>        // case-insensitive
    pub fn query_param(&self, name: &str) -> Option<&str>
    pub fn json(&self) -> Result<Value, JsonError>          // empty body -> Ok({})
}
pub struct Response { pub status: u16, pub body: Value }
impl Response {
    pub fn json(status: u16, body: Value) -> Self
    pub fn ok(body: Value) -> Self
    pub fn error(status: u16, code: &str, message: &str, service: &str,
                 retryable: bool, context: Value) -> Self   // CONVENTIONS §1.2 envelope
}
pub struct ServerConfig { pub service: String, pub protocol_error_code: String,
                          pub max_body_bytes: usize /*1 MiB*/, pub read_timeout_ms: u64 /*5000*/ }
impl ServerConfig { pub fn new(service: &str, protocol_error_code: &str) -> Self }
pub type Handler = Arc<dyn Fn(&Request) -> Response + Send + Sync>;
pub fn serve(bind: &str, port: u16, cfg: ServerConfig, handler: Handler) -> io::Result<ServerHandle>
pub struct ServerHandle;  impl ServerHandle { pub fn port(&self) -> u16; pub fn join(self) }
pub fn request<A: ToSocketAddrs>(addr: A, method: &str, path: &str, body: Option<&Value>,
                                 timeout_ms: u64) -> io::Result<(u16, Value)>   // client for tests + outbound calls
pub fn reason_phrase(status: u16) -> &'static str
```
`serve` returns immediately; port `0` binds an ephemeral port (read it via `.port()`).
`main.rs` calls `.join()` to block. Protocol-level failures (bad request line,
oversized body, chunked encoding) are answered by the server itself using
`protocol_error_code`; everything else is the handler's job.

## crypto.rs
```rust
pub struct Sha256;  impl Sha256 { pub fn new() -> Self; pub fn update(&mut self, data: &[u8]);
                                  pub fn finalize(self) -> [u8; 32] }
pub fn sha256(data: &[u8]) -> [u8; 32]                pub fn sha256_hex(data: &[u8]) -> String
pub fn hmac_sha256(key: &[u8], data: &[u8]) -> [u8; 32]
pub fn hmac_sha256_hex(key: &[u8], data: &[u8]) -> String
pub fn base64url_encode(data: &[u8]) -> String         // unpadded
pub fn base64url_decode(s: &str) -> Result<Vec<u8>, CryptoError>   // padding optional; rejects + and /
pub fn hex_encode(data: &[u8]) -> String               pub fn hex_decode(s: &str) -> Result<Vec<u8>, CryptoError>
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool    // use for every MAC/signature comparison
pub fn random_bytes(n: usize) -> io::Result<Vec<u8>>   // /dev/urandom; errors rather than degrading
pub fn random_token(n: usize) -> io::Result<String>    // n random bytes as base64url (jti, kid, nonces)
pub struct CryptoError { pub message: String }         // Display, Error
```

## Wiring pattern for a service's `http.rs`
```rust
use crate::router::{Resolution, Router};
use crate::server::{serve, Handler, Request, Response, ServerConfig, ServerHandle};
use std::sync::Arc;

pub fn build_router() -> Router {
    let mut r = Router::new();
    r.add("GET", "/healthz", "healthz")
     .add("GET", "/readyz", "readyz")
     .add("POST", "/token", "token_issue")
     .add("GET", "/keys/{kid}", "key_get");
    r
}

pub fn start(bind: &str, port: u16, mgr: Arc<AuthManager>) -> std::io::Result<ServerHandle> {
    let router = build_router();
    let handler: Handler = Arc::new(move |req: &Request| match router.resolve(&req.method, &req.path) {
        Resolution::Found { name, params } => match name {
            "healthz" => healthz(),
            "readyz" => readyz(&mgr),
            "token_issue" => token_issue(&mgr, req),
            "key_get" => key_get(&mgr, params.get("kid").unwrap_or("")),
            _ => Response::error(500, "AUT-999", "Unrouted handler name.", SERVICE, false, Value::Null),
        },
        Resolution::MethodNotAllowed { allowed } => Response::error(405, "AUT-003", "Method not allowed.",
            SERVICE, false, Value::object(vec![("allowed", allowed.join(", ").into())])),
        Resolution::NotFound => Response::error(404, "AUT-002", "No such route.", SERVICE, false, Value::Null),
    });
    serve(bind, port, ServerConfig::new(SERVICE, "AUT-001"), handler)
}
```
Handlers are plain functions `fn(&Manager, &Request) -> Response`; map each
`Error` variant to `Response::error(status, code, msg, SERVICE, retryable, ctx)`.
In tests: `let h = http::start("127.0.0.1", 0, mgr)?;` then
`server::request(("127.0.0.1", h.port()), "POST", "/token", Some(&body), 2000)`.