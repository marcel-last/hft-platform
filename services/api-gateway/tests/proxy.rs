//! Proxy semantics: verbatim pass-through of upstream status+body, bodyless
//! methods never forward a body, query strings are forwarded, and transport
//! failures (unreachable upstream) map to `API-502`/`API-503`.
//!
//! All tests run against the fake transport (no sockets). S12 (port 7720)
//! always returns a valid flat-claims body so the pipeline reaches the proxy.

mod common;

use std::sync::Arc;

use apigw::core::OutboundResult;
use apigw::errors::ApiError;
use apigw::json::Value;
use common::*;

const EXP: i64 = 1_788_000_000_000_000_000;

/// Gateway whose S12 hop returns a valid token carrying `scopes`, and whose
/// upstream hop (any non-7720 port) returns `status` + `body`.
fn proxy_gw(scopes: Vec<String>, status: u16, body: Value) -> (apigw::core::Gateway, Arc<FakeTransport>) {
    let fake = FakeTransport::new(move |c| {
        if c.addr.1 == 7720 {
            let refs: Vec<&str> = scopes.iter().map(|s| s.as_str()).collect();
            Ok(OutboundResult { status: 200, body: s12_verify_ok("u", "j", "k", &refs, EXP) })
        } else {
            Ok(OutboundResult { status, body: body.clone() })
        }
    });
    (gateway(&fake), fake)
}

#[test]
fn verbatim_200_pass_through() {
    let body = Value::object(vec![("symbol", "ES".into()), ("pnl", 10.5f64.into())]);
    let (gw, fake) = proxy_gw(vec!["read".to_string()], 200, body);
    let (st, out) = gw.handle("GET", "/portfolio/pnl/ES", "", Some("Bearer t"), None);
    assert_eq!(st, 200, "upstream 200 must pass through (body {})", out.to_json());
    assert_eq!(out.get("symbol").and_then(|v| v.as_str()), Some("ES"));
    assert_eq!(out.get("pnl").and_then(|v| v.as_f64()), Some(10.5));

    // The upstream hop targeted S9 (7690) with the rewritten path and no body.
    let up = fake.to_port(7690);
    assert_eq!(up.len(), 1);
    assert_eq!(up[0].method, "GET");
    assert_eq!(up[0].path, "/pnl/ES");
    assert!(up[0].body.is_none(), "GET must not carry a body upstream");

    let s = gw.stats_snapshot();
    assert_eq!(s.proxied, 1);
    assert_eq!(s.auth_ok, 1);
}

#[test]
fn stl_409_envelope_forwarded_unchanged() {
    // If S14 answers 409 STL-205, the gateway forwards 409 + the STL-205
    // envelope verbatim — it does NOT re-wrap it in an API- envelope.
    let stl = upstream_error("settlement-service", "STL-205");
    let (gw, fake) = proxy_gw(vec!["write".to_string()], 409, stl);
    let settle_body = Value::object(vec![
        ("date", "2026-01-01".into()),
        ("fills", Value::Array(vec![Value::object(vec![("fill_id", "F1".into())])])),
    ]);
    let (st, out) = gw.handle("POST", "/settlement/settle", "", Some("Bearer t"), Some(settle_body.clone()));
    assert_eq!(st, 409, "S14 409 must pass through verbatim (body {})", out.to_json());
    assert_eq!(out.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()), Some("STL-205"));
    assert_eq!(out.get("error").and_then(|e| e.get("service")).and_then(|s| s.as_str()), Some("settlement-service"));

    // The POST body was forwarded to S14 with the rewritten path.
    let up = fake.to_port(7740);
    assert_eq!(up.len(), 1);
    assert_eq!(up[0].method, "POST");
    assert_eq!(up[0].path, "/settle");
    assert_eq!(up[0].body.as_ref(), Some(&settle_body));

    let s = gw.stats_snapshot();
    assert_eq!(s.proxied, 1);
    assert_eq!(s.proxy_upstream_errors, 0);
}

#[test]
fn upstream_500_passes_through_verbatim() {
    // An upstream that ANSWERS with its own 5xx is passed through verbatim
    // (distinct from a transport failure, which the gateway wraps).
    let s9err = Value::object(vec![(
        "error",
        Value::object(vec![
            ("code", "PFA-500".into()),
            ("message", "analytics internal error.".into()),
            ("service", "portfolio-analytics".into()),
            ("retryable", true.into()),
            ("context", Value::object(vec![])),
        ]),
    )]);
    let (gw, _fake) = proxy_gw(vec!["read".to_string()], 500, s9err);
    let (st, out) = gw.handle("GET", "/portfolio/metrics", "", Some("Bearer t"), None);
    assert_eq!(st, 500, "upstream 500 passes through verbatim");
    assert_eq!(out.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()), Some("PFA-500"));
}

#[test]
fn bodyless_methods_skip_body() {
    let (gw, fake) = proxy_gw(vec!["read".to_string()], 200, Value::object(vec![]));
    // A caller smuggles a body onto a GET; it must NOT be forwarded upstream.
    let bogus = Value::object(vec![("x", 1i64.into())]);
    let (st, _) = gw.handle("GET", "/portfolio/pnl", "", Some("Bearer t"), Some(bogus));
    assert_eq!(st, 200);
    let up = fake.to_port(7690);
    assert_eq!(up.len(), 1);
    assert!(up[0].body.is_none(), "a GET must never forward a body upstream");
}

#[test]
fn query_string_forwarded() {
    let (gw, fake) = proxy_gw(vec!["read".to_string()], 200, Value::object(vec![]));
    let (st, _) = gw.handle("GET", "/settlement/discrepancies", "date=2026-01-01&limit=5", Some("Bearer t"), None);
    assert_eq!(st, 200);
    let up = fake.to_port(7740);
    assert_eq!(up.len(), 1);
    assert_eq!(up[0].path, "/discrepancies?date=2026-01-01&limit=5");

    // No query → bare rewritten path (no trailing `?`).
    let (st2, _) = gw.handle("GET", "/portfolio/var", "", Some("Bearer t"), None);
    assert_eq!(st2, 200);
    let up2 = fake.to_port(7690);
    assert_eq!(up2.last().map(|c| c.path.as_str()), Some("/var"));
}

#[test]
fn unreachable_upstream_is_retryable_api502_503() {
    // A transport failure (socket refused) is retryable → 503 API-502.
    let fake = FakeTransport::new(move |c| {
        if c.addr.1 == 7720 {
            Ok(OutboundResult { status: 200, body: s12_verify_ok("u", "j", "k", &["write"], EXP) })
        } else {
            Err(ApiError::upstream("settlement-service", "unreachable: Connection refused", true))
        }
    });
    let gw = gateway(&fake);
    let (st, body) = gw.handle("POST", "/settlement/settle", "", Some("Bearer t"), Some(Value::object(vec![("date", "2026-01-01".into())])));
    assert_api_envelope(st, &body, 503, "API-502", true);
    assert_eq!(ctx(&body, "service").as_deref(), Some("settlement-service"));
    let s = gw.stats_snapshot();
    assert_eq!(s.proxy_upstream_errors, 1);
    assert_eq!(s.proxied, 0);
}

#[test]
fn non_retryable_upstream_failure_is_502() {
    // A non-retryable transport error → 502 (not 503), still API-502.
    let fake = FakeTransport::new(move |c| {
        if c.addr.1 == 7720 {
            Ok(OutboundResult { status: 200, body: s12_verify_ok("u", "j", "k", &["read"], EXP) })
        } else {
            Err(ApiError::upstream("portfolio-analytics", "read timeout", false))
        }
    });
    let gw = gateway(&fake);
    let (st, body) = gw.handle("GET", "/portfolio/pnl", "", Some("Bearer t"), None);
    assert_api_envelope(st, &body, 502, "API-502", false);
    assert_eq!(ctx(&body, "service").as_deref(), Some("portfolio-analytics"));
}

#[test]
fn auth_hop_is_separate_from_proxy_hop() {
    // A single proxied request makes exactly two hops: 1 to S12, 1 to the
    // upstream. Neither may duplicate.
    let (gw, fake) = proxy_gw(vec!["read".to_string()], 200, Value::object(vec![("ok", true.into())]));
    let _ = gw.handle("GET", "/portfolio/history", "", Some("Bearer t"), None);
    assert_eq!(fake.to_port(7720).len(), 1, "exactly one S12 verify hop");
    assert_eq!(fake.to_port(7690).len(), 1, "exactly one S9 proxy hop");
    assert!(fake.to_port(7740).is_empty(), "no settlement hop for a portfolio route");
}
