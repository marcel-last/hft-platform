//! Domain types for the gateway: the static route table, upstream identifiers,
//! the claims extracted from a validated token, gateway statistics and the
//! bearer-token parse.
//!
//! The route table is the heart of the gateway: it maps an inbound
//! `(method, path)` to an upstream service and the scopes the caller must hold.
//! Paths are matched by longest prefix so `/portfolio/pnl/ES` resolves to the
//! most specific registered prefix.

use crate::json::Value;

/// The two internal upstreams the gateway can forward to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Upstream {
    /// S9 portfolio-analytics.
    Portfolio,
    /// S14 settlement-service.
    Settlement,
}

impl Upstream {
    /// Stable short identifier used in logs and stats.
    pub fn id(&self) -> &'static str {
        match self {
            Upstream::Portfolio => "portfolio-analytics",
            Upstream::Settlement => "settlement-service",
        }
    }

    /// The inbound path prefix this upstream owns (excluding the leading `/`).
    pub fn prefix(&self) -> &'static str {
        match self {
            Upstream::Portfolio => "portfolio",
            Upstream::Settlement => "settlement",
        }
    }

    /// `true` when the caller supplied this upstream's path prefix.
    pub fn matches_path(&self, path: &str) -> bool {
        let p = self.prefix();
        path == format!("/{}", p).as_str() || path.starts_with(&format!("/{}", p))
    }
}

/// The set of scopes a caller must hold to use a route.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    /// Read-only access to data endpoints.
    Read,
    /// Write access (settlement, order mutation).
    Write,
}

impl Scope {
    pub fn as_str(&self) -> &'static str {
        match self {
            Scope::Read => "read",
            Scope::Write => "write",
        }
    }

    /// Parse a scope string; unknown strings return `None`.
    pub fn parse(s: &str) -> Option<Scope> {
        match s {
            "read" => Some(Scope::Read),
            "write" => Some(Scope::Write),
            _ => None,
        }
    }
}

/// Claims extracted from a token that S12 `auth-service` accepted. The gateway
/// does not re-verify signatures itself — it trusts S12's verdict and only
/// reads these fields for authorization + correlation.
#[derive(Debug, Clone)]
pub struct VerifiedClaims {
    /// Subject (client identity).
    pub sub: String,
    /// Token id (`jti`).
    pub jti: String,
    /// Signing key id (`kid`).
    pub kid: String,
    /// Scope list granted on the token.
    pub scopes: Vec<String>,
    /// Expiry (ns since epoch), if present.
    pub exp_ns: Option<i64>,
}

impl VerifiedClaims {
    /// `true` when the token carries the given scope.
    pub fn has_scope(&self, scope: Scope) -> bool {
        self.scopes.iter().any(|s| s == scope.as_str())
    }

    /// The `scopes` claim as a JSON array (for the `/token` introspection view).
    pub fn scopes_json(&self) -> Value {
        Value::Array(self.scopes.iter().map(|s| Value::String(s.clone())).collect())
    }
}

/// A single route entry.
#[derive(Debug, Clone)]
pub struct Route {
    /// Stable id (e.g. `"settle-post"`).
    pub id: &'static str,
    /// HTTP method(s) this route accepts (e.g. `&["POST"]`).
    pub methods: &'static [&'static str],
    /// Path prefix (leading `/` included, trailing `/` optional).
    pub prefix: &'static str,
    /// Upstream the request is forwarded to.
    pub upstream: Upstream,
    /// Scopes the caller must hold.
    pub scopes: &'static [Scope],
}

/// The result of resolving an inbound request against the route table.
#[derive(Debug, Clone, Copy)]
pub enum RouteResolution {
    /// Matched a specific (scoped) route.
    Routed(&'static Route),
    /// The prefix was recognized but the method was not allowed on it.
    MethodNotAllowed,
    /// The path prefix is not owned by any upstream.
    NoRoute,
}

/// The full static route table.
pub struct RouteTable;

impl RouteTable {
    /// The ordered list of routes, most-specific first.
    pub fn routes() -> &'static [Route] {
        &[
            // --- settlement (S14) — write endpoints first (longest prefixes win ties) ---
            Route { id: "settle-post", methods: &["POST"], prefix: "/settlement/settle", upstream: Upstream::Settlement, scopes: &[Scope::Write] },
            Route { id: "finalize-post", methods: &["POST"], prefix: "/settlement/finalize", upstream: Upstream::Settlement, scopes: &[Scope::Write] },
            Route { id: "ingest-post", methods: &["POST"], prefix: "/settlement/ingest", upstream: Upstream::Settlement, scopes: &[Scope::Write] },
            Route { id: "reports-get", methods: &["GET"], prefix: "/settlement/reports", upstream: Upstream::Settlement, scopes: &[Scope::Read] },
            Route { id: "runs-get", methods: &["GET"], prefix: "/settlement/runs", upstream: Upstream::Settlement, scopes: &[Scope::Read] },
            Route { id: "discrepancies-get", methods: &["GET"], prefix: "/settlement/discrepancies", upstream: Upstream::Settlement, scopes: &[Scope::Read] },
            // --- portfolio (S9) ---
            Route { id: "pnl-get", methods: &["GET"], prefix: "/portfolio/pnl", upstream: Upstream::Portfolio, scopes: &[Scope::Read] },
            Route { id: "metrics-get", methods: &["GET"], prefix: "/portfolio/metrics", upstream: Upstream::Portfolio, scopes: &[Scope::Read] },
            Route { id: "var-get", methods: &["GET"], prefix: "/portfolio/var", upstream: Upstream::Portfolio, scopes: &[Scope::Read] },
            Route { id: "attribution-get", methods: &["GET"], prefix: "/portfolio/attribution", upstream: Upstream::Portfolio, scopes: &[Scope::Read] },
            Route { id: "history-get", methods: &["GET"], prefix: "/portfolio/history", upstream: Upstream::Portfolio, scopes: &[Scope::Read] },
        ]
    }

    /// `true` when `path` is exactly the route prefix or sits under it at a
    /// `/` boundary (so `/settlement/reports/x` matches `/settlement/reports`
    /// but `/settlement/reportsXYZ` does not).
    fn under_prefix(path: &str, prefix: &str) -> bool {
        path == prefix || (path.starts_with(prefix) && path.as_bytes().get(prefix.len()) == Some(&b'/'))
    }

    /// The longest route prefix that `path` sits under, ignoring method. Used
    /// to disambiguate 404 (no prefix) from 405 (prefix, wrong method).
    fn find_route_by_prefix(path: &str) -> Option<&'static Route> {
        let norm = path.strip_suffix('/').filter(|p| !p.is_empty()).unwrap_or(path);
        let mut best: Option<&'static Route> = None;
        for r in Self::routes() {
            if !Self::under_prefix(norm, r.prefix) { continue; }
            if best.map(|b| r.prefix.len() > b.prefix.len()).unwrap_or(true) {
                best = Some(r);
            }
        }
        best
    }

    /// Resolve `(method, path)` to a route. Longest prefix wins; the matched
    /// method must be in the route's allow-list. Only upstream routes live in
    /// this table — the gateway's local endpoints (healthz/readyz/routes/stats)
    /// are answered by `http.rs` before routing.
    pub fn resolve(method: &str, path: &str) -> RouteResolution {
        match Self::find_route_by_prefix(path) {
            Some(route) if route.methods.iter().any(|m| *m == method) => RouteResolution::Routed(route),
            Some(_) => RouteResolution::MethodNotAllowed,
            None => RouteResolution::NoRoute,
        }
    }

    /// The HTTP methods permitted on the prefix that owns `path`, or `None`
    /// when no prefix owns it. Used to populate the `allowed` field of a 405.
    pub fn allowed_methods(path: &str) -> Option<Vec<&'static str>> {
        Self::find_route_by_prefix(path).map(|r| r.methods.to_vec())
    }

    /// Rewrite an inbound path for the upstream: strip the gateway's
    /// first-segment prefix (`/settlement/...` → `/...`, `/portfolio/...` →
    /// `/...`) and keep the remainder plus the query intact.
    pub fn rewrite_path(upstream: Upstream, path: &str) -> String {
        let prefix = format!("/{}", upstream.prefix());
        if path == prefix {
            "/".to_string()
        } else if let Some(rest) = path.strip_prefix(&prefix) {
            rest.to_string() // rest starts with '/', e.g. "/reports/2026-01-01"
        } else {
            path.to_string()
        }
    }
}

/// Parse an `Authorization` header value into a bearer token, if present and
/// well-formed. Returns `None` for missing/malformed headers.
pub fn parse_bearer(header: Option<&str>) -> Option<String> {
    let h = header?;
    let t = h.trim();
    let rest = t.strip_prefix("Bearer ").or_else(|| t.strip_prefix("bearer "))?;
    let tok = rest.trim();
    if tok.is_empty() { return None; }
    Some(tok.to_string())
}

/// Gateway request counters, mirrored to `GET /stats`.
#[derive(Debug, Clone, Default)]
pub struct GatewayStats {
    pub requests_total: u64,
    pub auth_ok: u64,
    pub auth_rejected: u64,
    pub scope_denied: u64,
    pub auth_upstream_errors: u64,
    pub route_hits: u64,
    pub route_404: u64,
    pub route_405: u64,
    pub proxied: u64,
    pub proxy_upstream_errors: u64,
}

impl GatewayStats {
    /// Serialize to a JSON object (field order matches struct declaration).
    pub fn to_json(&self) -> Value {
        Value::object(vec![
            ("requests_total", self.requests_total.into()),
            ("auth_ok", self.auth_ok.into()),
            ("auth_rejected", self.auth_rejected.into()),
            ("scope_denied", self.scope_denied.into()),
            ("auth_upstream_errors", self.auth_upstream_errors.into()),
            ("route_hits", self.route_hits.into()),
            ("route_404", self.route_404.into()),
            ("route_405", self.route_405.into()),
            ("proxied", self.proxied.into()),
            ("proxy_upstream_errors", self.proxy_upstream_errors.into()),
        ])
    }
}
