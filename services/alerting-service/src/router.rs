//! alerting_service — HTTP route matching.
//!
//! A small method+path router with `{param}` segments, mirroring the other
//! services' pattern.  Routes are matched in registration order; the first
//! match wins and receives captured path parameters plus the parsed query.

use std::collections::HashMap;

/// The result of dispatching one request: HTTP status + JSON body bytes.
pub struct Response {
    pub status: u16,
    pub body: Vec<u8>,
}

impl Response {
    pub fn json(status: u16, value: &crate::json::Value) -> Self {
        Response {
            status,
            body: value.to_json().into_bytes(),
        }
    }

    pub fn error(err: &crate::errors::Error) -> Self {
        Response::json(err.http_status, &err.to_envelope())
    }
}

#[derive(Clone)]
enum Seg {
    Lit(String),
    Param(String),
}

/// A single registered route.
#[derive(Clone)]
struct Route {
    method: String,
    segments: Vec<Seg>,
    handler: crate::http::Handler,
}

impl Route {
    fn new(method: &str, pattern: &str, handler: crate::http::Handler) -> Self {
        let segments = pattern
            .trim_start_matches('/')
            .split('/')
            .map(|part| {
                if part.starts_with('{') && part.ends_with('}') {
                    Seg::Param(part[1..part.len() - 1].to_string())
                } else {
                    Seg::Lit(part.to_string())
                }
            })
            .collect();
        Route {
            method: method.to_uppercase(),
            segments,
            handler,
        }
    }

    /// Match a request path against this route (method already checked).
    fn match_path(&self, path: &str) -> Option<HashMap<String, String>> {
        let parts: Vec<&str> = path.trim_start_matches('/').split('/').collect();
        if parts.len() != self.segments.len() {
            return None;
        }
        let mut params = HashMap::new();
        for (part, seg) in parts.iter().zip(self.segments.iter()) {
            match seg {
                Seg::Lit(lit) => {
                    if lit != part {
                        return None;
                    }
                }
                Seg::Param(name) => {
                    params.insert(name.clone(), (*part).to_string());
                }
            }
        }
        Some(params)
    }
}

/// The router: an ordered list of routes.
#[derive(Clone)]
pub struct Router {
    routes: Vec<Route>,
}

impl Default for Router {
    fn default() -> Self {
        Router::new()
    }
}

impl Router {
    pub fn new() -> Self {
        Router { routes: Vec::new() }
    }

    pub fn add(&mut self, method: &str, pattern: &str, handler: crate::http::Handler) {
        self.routes.push(Route::new(method, pattern, handler));
    }

    /// Dispatch a request.  Returns the response to send back to the client.
    pub fn dispatch(
        &self,
        method: &str,
        path: &str,
        query: HashMap<String, String>,
        body: &str,
        manager: &crate::core::SharedManager,
    ) -> Response {
        let method = method.to_uppercase();
        for route in self.routes.iter() {
            if route.method != method {
                continue;
            }
            if let Some(params) = route.match_path(path) {
                return (route.handler)(RequestCtx {
                    method: method.clone(),
                    path,
                    params,
                    query,
                    body,
                    manager,
                });
            }
        }
        Response::error(&crate::errors::Error::not_found(&method, path))
    }

    /// Register the standard alerting-service routes.  The literal `/alerts/ack`
    /// and `/alerts/dispatch` are registered BEFORE the parameterized
    /// `/alerts/{id}` so the fixed segments win over the catch-all.
    pub fn with_default_routes() -> Self {
        let mut r = Router::new();
        use crate::http::{
            h_ack, h_alert, h_alerts, h_alerts_dispatch, h_alerts_list, h_healthz, h_readyz,
            h_resolve, h_stats,
        };
        r.add("GET", "/healthz", h_healthz);
        r.add("GET", "/readyz", h_readyz);
        r.add("POST", "/alerts", h_alerts);
        r.add("GET", "/alerts", h_alerts_list);
        r.add("GET", "/alerts/dispatch", h_alerts_dispatch);
        r.add("POST", "/alerts/ack", h_ack);
        r.add("POST", "/alerts/{id}/resolve", h_resolve);
        r.add("GET", "/alerts/{id}", h_alert);
        r.add("GET", "/stats", h_stats);
        r
    }
}

/// Context handed to each handler.
pub struct RequestCtx<'a> {
    pub method: String,
    pub path: &'a str,
    pub params: HashMap<String, String>,
    pub query: HashMap<String, String>,
    pub body: &'a str,
    pub manager: &'a crate::core::SharedManager,
}
