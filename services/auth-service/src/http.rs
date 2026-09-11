//! S12 auth-service HTTP handlers (request handling ONLY — the accept loop,
//! request parsing and response writing live in the template `server.rs`).
//!
//! Endpoints:
//!   GET  /healthz  -> liveness
//!   GET  /readyz   -> readiness (needs an active signing key)
//!   POST /token    -> mint an HS256 JWT
//!   POST /verify   -> verify a JWT (optional required-scope check)
//!   POST /revoke   -> revoke a jti (idempotent)
//!   GET  /keys     -> JWKS-style public key view (no secrets)
//!   GET  /stats    -> counters

use crate::config::{SERVICE_NAME, SERVICE_VERSION};
use crate::core::AuthManager;
use crate::errors::AuthError;
use crate::json::Value;
use crate::router::{Resolution, Router};
use crate::server::{serve, Handler, Request, Response, ServerConfig, ServerHandle};
use std::sync::Arc;

/// Map an `AuthError` to the standard CONVENTIONS §1.2 error envelope.
fn err(e: &AuthError) -> Response {
    Response::error(e.status(), e.code(), e.message(), SERVICE_NAME, e.retryable(), e.context().clone())
}

fn healthz() -> Response {
    Response::ok(Value::object(vec![
        ("status", Value::from("ok")),
        ("service", Value::from(SERVICE_NAME)),
        ("version", Value::from(SERVICE_VERSION)),
    ]))
}

fn readyz(mgr: &AuthManager) -> Response {
    let (ready, reasons) = mgr.ready();
    let list: Vec<Value> = reasons.iter().map(|r| Value::from(r.as_str())).collect();
    Response::ok(Value::object(vec![
        ("status", Value::from(if ready { "ready" } else { "not_ready" })),
        ("reasons", Value::from(list)),
    ]))
}

/// Parse a `scopes` body field: absent/null -> empty, array of strings, else error.
fn parse_scopes(body: &Value) -> Result<Vec<String>, AuthError> {
    match body.get("scopes") {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(v) => {
            let arr = v.as_array().ok_or_else(|| AuthError::malformed_body("'scopes' must be an array"))?;
            let mut out: Vec<String> = Vec::with_capacity(arr.len());
            for s in arr {
                let t = s
                    .as_str()
                    .ok_or_else(|| AuthError::malformed_body("'scopes' must contain only strings"))?;
                if t.trim().is_empty() {
                    return Err(AuthError::malformed_body("'scopes' entries must be non-empty"));
                }
                out.push(t.to_string());
            }
            Ok(out)
        }
    }
}

fn token_issue(mgr: &AuthManager, req: &Request) -> Response {
    let body = match req.json() {
        Ok(b) => b,
        Err(e) => return err(&AuthError::malformed_body(&e.message)),
    };
    let sub = match body.get("sub") {
        Some(v) => match v.as_str() {
            Some(s) if !s.trim().is_empty() => s.to_string(),
            _ => return err(&AuthError::missing_subject("missing or empty 'sub'")),
        },
        None => return err(&AuthError::missing_subject("'sub' is required")),
    };
    let scopes = match parse_scopes(&body) {
        Ok(s) => s,
        Err(e) => return err(&e),
    };
    let ttl = match body.get("ttl_ns") {
        None | Some(Value::Null) => None,
        Some(v) => match v.as_i64() {
            Some(t) => Some(t),
            None => return err(&AuthError::bad_ttl("ttl_ns must be an integer")),
        },
    };
    match mgr.issue(&sub, &scopes, ttl) {
        Ok(tok) => Response::ok(tok.to_json()),
        Err(e) => err(&e),
    }
}

fn token_verify(mgr: &AuthManager, req: &Request) -> Response {
    let body = match req.json() {
        Ok(b) => b,
        Err(e) => return err(&AuthError::malformed_body(&e.message)),
    };
    let token = match body.get("token") {
        Some(v) => match v.as_str() {
            Some(s) if !s.trim().is_empty() => s.to_string(),
            _ => return err(&AuthError::missing_token()),
        },
        None => return err(&AuthError::missing_token()),
    };
    let scope = match body.get("scope") {
        None | Some(Value::Null) => None,
        Some(v) => match v.as_str() {
            Some(s) => Some(s),
            None => return err(&AuthError::malformed_body("'scope' must be a string")),
        },
    };
    match mgr.verify(&token, scope) {
        Ok(vc) => {
            let mut claims = vc.to_json();
            claims.set("valid", Value::from(true));
            Response::ok(claims)
        }
        Err(e) => err(&e),
    }
}

fn token_revoke(mgr: &AuthManager, req: &Request) -> Response {
    let body = match req.json() {
        Ok(b) => b,
        Err(e) => return err(&AuthError::malformed_body(&e.message)),
    };
    let jti = match body.get("jti") {
        Some(v) => match v.as_str() {
            Some(s) if !s.trim().is_empty() => s.to_string(),
            _ => return err(&AuthError::missing_jti()),
        },
        None => return err(&AuthError::missing_jti()),
    };
    match mgr.revoke(&jti) {
        Ok(r) => Response::ok(Value::object(vec![
            ("revoked", Value::from(true)),
            ("jti", Value::from(r.jti.as_str())),
            ("created", Value::from(r.created)),
        ])),
        Err(e) => err(&e),
    }
}

fn keys_list(mgr: &AuthManager) -> Response {
    Response::ok(Value::from(mgr.keys_public()))
}

fn stats_view(mgr: &AuthManager) -> Response {
    let s = mgr.stats();
    Response::ok(Value::object(vec![
        ("issued", Value::from(s.issued)),
        ("verified_ok", Value::from(s.verified_ok)),
        ("verified_fail", Value::from(s.verified_fail)),
        ("revoked", Value::from(s.revoked)),
        ("rotations", Value::from(s.rotations)),
    ]))
}

/// Route table for the service.
pub fn build_router() -> Router {
    let mut r = Router::new();
    r.add("GET", "/healthz", "healthz")
        .add("GET", "/readyz", "readyz")
        .add("POST", "/token", "token_issue")
        .add("POST", "/verify", "token_verify")
        .add("POST", "/revoke", "token_revoke")
        .add("GET", "/keys", "keys_list")
        .add("GET", "/stats", "stats_view");
    r
}

/// Start the HTTP server (bind "0.0.0.0" + port 0 for ephemeral in tests).
pub fn start(bind: &str, port: u16, mgr: Arc<AuthManager>) -> std::io::Result<ServerHandle> {
    let router = build_router();
    let handler: Handler = Arc::new(move |req: &Request| match router.resolve(&req.method, &req.path) {
        Resolution::Found { name, .. } => match name {
            "healthz" => healthz(),
            "readyz" => readyz(&mgr),
            "token_issue" => token_issue(&mgr, req),
            "token_verify" => token_verify(&mgr, req),
            "token_revoke" => token_revoke(&mgr, req),
            "keys_list" => keys_list(&mgr),
            "stats_view" => stats_view(&mgr),
            _ => err(&AuthError::no_route(&req.method, &req.path)),
        },
        Resolution::MethodNotAllowed { allowed } => err(&AuthError::method_not_allowed(&allowed)),
        Resolution::NotFound => err(&AuthError::no_route(&req.method, &req.path)),
    });
    serve(bind, port, ServerConfig::new(SERVICE_NAME, "AUT-001"), handler)
}
