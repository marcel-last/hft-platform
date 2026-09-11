//! HTTP handler layer for the gateway.
//!
//! The template `Router` serves only the four local endpoints
//! (`/healthz`, `/readyz`, `/routes`, `/stats`). Everything else
//! falls through to `gateway.handle` which performs its own
//! longest-prefix route resolution, token verification, authorization,
//! and upstream proxy — producing the final `(status, body)`.

use std::sync::Arc;

use crate::config::SERVICE_NAME;
use crate::core::Gateway;
use crate::errors::ApiError;
use crate::json::Value;
use crate::router::{Resolution, Router};
use crate::server::{serve, Handler, Request, Response, ServerConfig, ServerHandle};

/// Build the local-only router. Upstream paths are NOT registered here;
/// they are handled by `gateway.handle` after this router returns `NotFound`.
pub fn build_router() -> Router {
    let mut r = Router::new();
    r.add("GET", "/healthz", "healthz")
     .add("GET", "/readyz", "readyz")
     .add("GET", "/routes", "routes")
     .add("GET", "/stats", "stats");
    r
}

/// Start the gateway HTTP listener on `bind:port`.
/// `port == 0` binds an ephemeral port (useful in tests).
pub fn start(
    bind: &str,
    port: u16,
    gateway: Gateway,
) -> std::io::Result<ServerHandle> {
    let router = build_router();
    let max_body = gateway.config.max_body_bytes;
    let gw = Arc::new(gateway);

    let handler: Handler = Arc::new(move |req: &Request| {
        match router.resolve(&req.method, &req.path) {
            Resolution::Found { name, .. } => match name {
                "healthz" => Response::ok(gw.healthz()),
                "readyz" => readyz_handler(&gw),
                "routes" => Response::ok(gw.routes_view()),
                "stats" => stats_handler(&gw),
                _ => Response::error(500, "API-999", "Unrouted handler name.", SERVICE_NAME, false, Value::Null),
            },
            // MethodNotAllowed on a local endpoint → 405.
            Resolution::MethodNotAllowed { allowed } => {
                let e = ApiError::MethodNotAllowed {
                    method: req.method.clone(),
                    path: req.path.clone(),
                    allowed,
                };
                Response::error(e.status(), e.code(), e.message(), SERVICE_NAME, e.retryable(), e.context())
            }
            // Everything else (including all upstream proxy paths) → gateway.handle
            Resolution::NotFound => proxy_handler(&gw, req),
        }
    });

    let cfg = ServerConfig {
        max_body_bytes: max_body,
        ..ServerConfig::new(SERVICE_NAME, "API-001")
    };

    serve(bind, port, cfg, handler)
}

/// `GET /readyz` — checks S12 auth-service reachability.
fn readyz_handler(gw: &Arc<Gateway>) -> Response {
    let (ready, reason) = gw.readyz();
    let body = if ready {
        Value::object(vec![
            ("status", "ready".into()),
            ("reasons", Value::Array(vec![])),
        ])
    } else {
        Value::object(vec![
            ("status", "not_ready".into()),
            ("reasons", Value::Array(vec![reason.into()])),
        ])
    };
    Response::ok(body)
}

/// `GET /stats` — gateway request counters.
fn stats_handler(gw: &Arc<Gateway>) -> Response {
    let s = gw.stats_snapshot();
    Response::ok(s.to_json())
}

/// All non-local paths: run the full gateway pipeline (route → verify →
/// authorize → proxy). Returns the upstream's status and body, or a
/// structured `API-` error envelope.
fn proxy_handler(gw: &Arc<Gateway>, req: &Request) -> Response {
    // Parse the body if one was sent.
    let body: Option<Value> = if req.body.is_empty() {
        None
    } else {
        match req.json() {
            Ok(v) => Some(v),
            Err(_) => {
                let e = ApiError::BadBody;
                return Response::error(e.status(), e.code(), e.message(), SERVICE_NAME, e.retryable(), e.context());
            }
        }
    };

    // The server template splits the target into path + query; we need the
    // original query string for forwarding.
    let query = req.query
        .iter()
        .map(|(k, v)| format!("{}={}", k, v))
        .collect::<Vec<_>>()
        .join("&");

    let (status, body_val) = gw.handle(
        &req.method,
        &req.path,
        &query,
        req.header("authorization"),
        body,
    );
    Response::json(status, body_val)
}
