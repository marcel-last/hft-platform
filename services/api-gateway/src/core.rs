//! Gateway core: the per-request pipeline and its supporting types.
//!
//! Pipeline (one inbound request):
//!   1. **Route** — longest-prefix match against the static route table
//!      (`models::RouteTable`). No prefix ⇒ `API-404`, wrong method ⇒ `API-405`.
//!   2. **Authenticate** — parse the `Authorization` header and call S12
//!      `auth-service` `POST /verify` through the `Transport`. A S12 `400`
//!      (bad signature, expired, revoked, …) ⇒ `API-202 TokenRejected`;
//!      a transport/5xx failure ⇒ `API-503 AuthUpstream` (retryable).
//!   3. **Authorize** — the subject's scopes must cover the route's scope set,
//!      else `API-203 Forbidden`.
//!   4. **Proxy** — rewrite the path (strip the gateway prefix) and forward the
//!      method, query and body to the upstream, transparently passing through
//!      its status code and body. Unreachable upstream ⇒ `API-503`, an
//!      upstream that answered with its own error ⇒ `API-502`.
//!
//! All upstream I/O goes through the `Transport` trait so integration tests can
//! inject scripted fakes; production uses `HttpTransport` (the template client).

use std::sync::{Arc, Mutex};

use crate::config::{GatewayConfig, SERVICE_NAME, SERVICE_VERSION};
use crate::errors::ApiError;
use crate::json::Value;
use crate::models::{parse_bearer, RouteResolution, RouteTable, Route, Upstream, VerifiedClaims, GatewayStats};
use crate::server;

/// One outbound hop: a method, a path (query included) and an optional body.
#[derive(Debug, Clone)]
pub struct Outbound {
    /// HTTP method, e.g. `"GET"`.
    pub method: String,
    /// Path plus (possibly empty) query string, e.g. `"/reports/2026-01-01?limit=5"`.
    pub path: String,
    /// Request body; `None` for bodyless calls.
    pub body: Option<Value>,
}

/// The result of one outbound hop.
#[derive(Debug, Clone)]
pub struct OutboundResult {
    /// Upstream HTTP status code.
    pub status: u16,
    /// Parsed JSON body (`Value::Null` when the upstream sent nothing JSON).
    pub body: Value,
}

/// Abstraction over upstream HTTP so the gateway's pipeline is testable without
/// real sockets.
pub trait Transport: Send + Sync {
    /// Send one request to `addr` and return the status + parsed body.
    fn call(&self, addr: (String, u16), out: Outbound, timeout_ms: u64) -> Result<OutboundResult, ApiError>;
}

/// Production transport: dials the socket with the template client.
pub struct HttpTransport;

impl Transport for HttpTransport {
    fn call(&self, addr: (String, u16), out: Outbound, timeout_ms: u64) -> Result<OutboundResult, ApiError> {
        let peer = format!("{}:{}", addr.0, addr.1);
        let body_ref = out.body.as_ref();
        match server::request((addr.0, addr.1), &out.method, &out.path, body_ref, timeout_ms) {
            Ok((status, body)) => Ok(OutboundResult { status, body }),
            Err(e) => Err(ApiError::upstream(upstream_id(&peer), format!("unreachable: {}", e), true)),
        }
    }
}

/// Map a `(host, port)` back to the upstream's stable id for error context.
fn upstream_id(addr: &str) -> &'static str {
    // Ports are fixed by convention (S9 :7690, S14 :7740); fall back to a
    // generic id when neither matches.
    if addr.ends_with(":7690") { "portfolio-analytics" }
    else if addr.ends_with(":7740") { "settlement-service" }
    else { "upstream" }
}

/// A token-verification hop is a POST to S12 `/verify` whose 400 body carries
/// S12's rejection `code`/`reason` in its `context`.
pub struct VerifyCall {
    /// The bearer token being checked.
    pub token: String,
}

impl VerifyCall {
    /// Build the outbound hop for S12 `POST /verify`.
    pub fn to_outbound(&self) -> Outbound {
        let body = Value::object(vec![("token", self.token.clone().into())]);
        Outbound { method: "POST".to_string(), path: "/verify".to_string(), body: Some(body) }
    }
}

/// Classify S12's answer to a `POST /verify`.
///
/// * `Ok(claims)` — 2xx: token valid, claims extracted.
/// * `Err(TokenRejected)` — S12 answered 4xx with a well-formed error body
///   (bad signature, expired, revoked, …). Not retryable.
/// * `Err(AuthUpstream)` — transport failure or an S12 5xx / malformed answer.
///   Retryable.
pub fn classify_verify(result: &OutboundResult) -> Result<VerifiedClaims, ApiError> {
    if (200..300).contains(&result.status) {
        return verify_claims(&result.body).map_err(|d| ApiError::AuthUpstream {
            detail: format!("malformed S12 success body: {}", d),
        });
    }
    if (400..500).contains(&result.status) {
        // S12 rejected the token. Pull its error code/reason for our context.
        let err = result.body.get("error");
        let detail = match err {
            Some(e) => {
                let mut s = e.get("code").and_then(|c| c.as_str()).unwrap_or("rejected").to_string();
                if let Some(reason) = e.get("context").and_then(|c| c.get("reason")).and_then(|r| r.as_str()) {
                    s.push_str(": ");
                    s.push_str(reason);
                }
                s
            }
            None => format!("auth-service answered {} with a non-error body", result.status),
        };
        return Err(ApiError::TokenRejected { detail });
    }
    Err(ApiError::AuthUpstream {
        detail: format!("auth-service answered {}", result.status),
    })
}

/// Extract the claims we need from a S12 `/verify` success body.
fn verify_claims(body: &Value) -> Result<VerifiedClaims, String> {
    let valid = body.get("valid").and_then(|v| v.as_bool()).ok_or("missing `valid`")?;
    if !valid {
        return Err("valid=false on a 2xx verify".to_string());
    }
    // S12 answers with a FLAT claims object (top-level `sub`/`jti`/`scope`/`exp`
    // — see auth-service `VerifiedClaims::to_json`); there is no nested `claims`
    // wrapper. Accept a nested wrapper too so a future S12 re-wrap stays
    // compatible, and treat the top-level body as the default source.
    let claims = body.get("claims").unwrap_or(body);
    let get_str = |k: &str| claims.get(k).and_then(|v| v.as_str()).ok_or_else(|| format!("missing claim `{}`", k));
    let sub = get_str("sub")?;
    let jti = get_str("jti")?;
    let kid = get_str("kid")?;
    let exp_ns = claims.get("exp").and_then(|v| v.as_i64());
    let scopes = match claims.get("scope").or_else(|| claims.get("scopes")) {
        Some(Value::Array(items)) => items.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect(),
        Some(Value::String(s)) => vec![s.to_string()],
        Some(_) => Vec::new(),
        None => Vec::new(),
    };
    Ok(VerifiedClaims { sub: sub.to_string(), jti: jti.to_string(), kid: kid.to_string(), scopes, exp_ns })
}

/// The gateway: config + transport + a stats counter. Cheaply cloneable
/// (everything behind `Arc`) and shared by every handler thread.
#[derive(Clone)]
pub struct Gateway {
    /// Immutable configuration.
    pub config: GatewayConfig,
    /// Upstream I/O (production: `HttpTransport`; tests: a scripted fake).
    pub transport: Arc<dyn Transport>,
    /// Shared request counters.
    pub stats: Arc<Mutex<GatewayStats>>,
}

impl Gateway {
    /// Construct a gateway over the given config and transport.
    pub fn new(config: GatewayConfig, transport: Arc<dyn Transport>) -> Self {
        Gateway { config, transport, stats: Arc::new(Mutex::new(GatewayStats::default())) }
    }

    /// Apply a mutation to the shared stats (lock is best-effort; a poisoned
    /// lock must never take the gateway down).
    fn bump<F: FnOnce(&mut GatewayStats)>(&self, f: F) {
        if let Ok(mut s) = self.stats.lock() {
            f(&mut s);
        }
    }

    /// Resolve an upstream identifier to its `(host, port)`.
    fn upstream_addr(&self, up: Upstream) -> (String, u16) {
        match up {
            Upstream::Portfolio => self.config.portfolio.addr(),
            Upstream::Settlement => self.config.settlement.addr(),
        }
    }

    /// Verify a bearer token against S12 `auth-service` and extract its claims.
    pub fn verify_token(&self, token: &str) -> Result<VerifiedClaims, ApiError> {
        let addr = self.config.auth.addr();
        let out = VerifyCall { token: token.to_string() }.to_outbound();
        match self.transport.call(addr, out, self.config.verify_timeout_ms) {
            Ok(res) => match classify_verify(&res) {
                Ok(claims) => {
                    self.bump(|s| s.auth_ok += 1);
                    Ok(claims)
                }
                Err(e) => {
                    let is_reject = matches!(e, ApiError::TokenRejected { .. });
                    self.bump(|s| {
                        if is_reject { s.auth_rejected += 1; } else { s.auth_upstream_errors += 1; }
                    });
                    Err(e)
                }
            },
            Err(e) => {
                // A transport failure to S12 is an auth-service problem, not a
                // proxy upstream error: surface it as API-503 AuthUpstream
                // (retryable), preserving the transport's detail when present.
                self.bump(|s| s.auth_upstream_errors += 1);
                let detail = e
                    .context()
                    .get("detail")
                    .and_then(|d| d.as_str())
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| e.message().to_string());
                Err(ApiError::AuthUpstream { detail })
            }
        }
    }

    /// Ensure `claims` carries every scope the route requires.
    pub fn authorize(&self, route: &'static Route, claims: &VerifiedClaims) -> Result<(), ApiError> {
        for scope in route.scopes {
            if !claims.has_scope(*scope) {
                self.bump(|s| s.scope_denied += 1);
                return Err(ApiError::Forbidden { required: scope.as_str() });
            }
        }
        Ok(())
    }

    /// Run the full route → verify → authorize → proxy pipeline for one
    /// inbound request and return `(status, body)`. Never panics.
    pub fn handle(
        &self,
        method: &str,
        path: &str,
        query: &str,
        auth: Option<&str>,
        body: Option<Value>,
    ) -> (u16, Value) {
        self.bump(|s| s.requests_total += 1);

        // Bodyless methods never carry a body forward.
        let is_bodyless = method == "GET" || method == "HEAD" || method == "DELETE" || method == "OPTIONS";
        let body = if is_bodyless { None } else { body };

        // 1. Route.
        let route = match RouteTable::resolve(method, path) {
            RouteResolution::Routed(r) => r,
            RouteResolution::MethodNotAllowed => {
                self.bump(|s| s.route_405 += 1);
                let allowed = RouteTable::allowed_methods(path).unwrap_or_default();
                let e = ApiError::MethodNotAllowed {
                    method: method.to_string(),
                    path: path.to_string(),
                    allowed,
                };
                return (e.status(), e.envelope());
            }
            RouteResolution::NoRoute => {
                self.bump(|s| s.route_404 += 1);
                let e = ApiError::NoRoute { method: method.to_string(), path: path.to_string() };
                return (e.status(), e.envelope());
            }
        };
        self.bump(|s| s.route_hits += 1);

        // 2. Authenticate.
        let token = match parse_bearer(auth) {
            Some(t) => t,
            None => {
                self.bump(|s| s.auth_rejected += 1);
                let e = ApiError::MissingToken;
                return (e.status(), e.envelope());
            }
        };
        let claims = match self.verify_token(&token) {
            Ok(c) => c,
            Err(e) => return (e.status(), e.envelope()),
        };

        // 3. Authorize.
        if let Err(e) = self.authorize(route, &claims) {
            return (e.status(), e.envelope());
        }

        // 4. Proxy: rewrite the path (strip the gateway prefix), keep the
        //    query, and forward. The upstream's status/body pass through.
        let upstream_path = RouteTable::rewrite_path(route.upstream, path);
        let full_path = if query.is_empty() { upstream_path } else { format!("{}?{}", upstream_path, query) };
        let out = Outbound { method: method.to_string(), path: full_path, body };
        match self.transport.call(self.upstream_addr(route.upstream), out, self.config.proxy_timeout_ms) {
            Ok(res) => {
                self.bump(|s| s.proxied += 1);
                (res.status, res.body)
            }
            Err(e) => {
                self.bump(|s| s.proxy_upstream_errors += 1);
                (e.status(), e.envelope())
            }
        }
    }

    /// Readiness: the gateway is ready once it can reach `auth-service` to
    /// validate tokens. A dead auth service means every proxied request would
    /// fail, so it gates readiness.
    pub fn readyz(&self) -> (bool, String) {
        let addr = self.config.auth.addr();
        let out = Outbound { method: "GET".to_string(), path: "/healthz".to_string(), body: None };
        match self.transport.call(addr, out, self.config.verify_timeout_ms) {
            Ok(res) if (200..300).contains(&res.status) => (true, "auth-service reachable".to_string()),
            Ok(res) => (false, format!("auth-service answered {}", res.status)),
            Err(e) => (false, format!("auth-service unreachable: {}", e.message())),
        }
    }

    /// A consistent snapshot of the request counters.
    pub fn stats_snapshot(&self) -> GatewayStats {
        self.stats.lock().map(|s| s.clone()).unwrap_or_default()
    }

    /// The `/healthz` body: always `ok` — liveness is independent of readiness.
    pub fn healthz(&self) -> Value {
        Value::object(vec![
            ("status", "ok".into()),
            ("service", self.config.name.clone().into()),
            ("version", self.config.version.clone().into()),
        ])
    }

    /// The route table as a JSON array (served by `GET /routes`).
    pub fn routes_view(&self) -> Value {
        let items: Vec<Value> = RouteTable::routes().iter().map(|r| {
            Value::object(vec![
                ("id", r.id.into()),
                ("methods", Value::Array(r.methods.iter().map(|m| Value::String((*m).to_string())).collect())),
                ("path", format!("/{}/**", r.prefix.trim_start_matches('/')).into()),
                ("upstream", r.upstream.id().into()),
                ("scopes", Value::Array(r.scopes.iter().map(|s| Value::String(s.as_str().to_string())).collect())),
            ])
        }).collect();
        Value::object(vec![("service", SERVICE_NAME.into()), ("version", SERVICE_VERSION.into()), ("routes", Value::Array(items))])
    }
}
