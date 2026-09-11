//! Route resolution (longest prefix, trailing-slash normalization, rewrite)
//! and the 404/405-before-auth pipeline semantics.
//!
//! 404 (`API-404`) and 405 (`API-405`) are returned WITHOUT consulting
//! auth-service — the transport must never be touched for them.

mod common;

use apigw::core::OutboundResult;
use apigw::errors::ApiError;
use apigw::json::Value;
use apigw::models::{RouteResolution, RouteTable, Upstream};
use common::*;

fn resolve(method: &str, path: &str) -> RouteResolution {
    RouteTable::resolve(method, path)
}

#[test]
fn longest_prefix_wins() {
    let r = resolve("GET", "/settlement/reports/2026-01-01");
    match r {
        RouteResolution::Routed(rt) => {
            assert_eq!(rt.id, "reports-get");
            assert_eq!(rt.upstream, Upstream::Settlement);
        }
        other => panic!("expected Routed, got {:?}", other),
    }
    let r = resolve("GET", "/portfolio/pnl/ES");
    match r {
        RouteResolution::Routed(rt) => assert_eq!(rt.id, "pnl-get"),
        other => panic!("expected Routed, got {:?}", other),
    }
    // /settlement/settle (POST) must not fall through to a shorter prefix.
    match resolve("POST", "/settlement/settle") {
        RouteResolution::Routed(rt) => assert_eq!(rt.id, "settle-post"),
        other => panic!("expected Routed, got {:?}", other),
    }
}

#[test]
fn trailing_slash_normalized() {
    // Trailing slash is stripped before prefix matching.
    match resolve("GET", "/settlement/reports/2026-01-01/") {
        RouteResolution::Routed(rt) => assert_eq!(rt.id, "reports-get"),
        other => panic!("expected Routed, got {:?}", other),
    }
    // The bare prefix itself matches exactly.
    match resolve("GET", "/settlement/reports") {
        RouteResolution::Routed(rt) => assert_eq!(rt.id, "reports-get"),
        other => panic!("expected Routed, got {:?}", other),
    }
}

#[test]
fn boundary_not_matched() {
    // `/settlement/reportsXYZ` is not under the `/settlement/reports` prefix
    // (no `/` boundary) and no other prefix owns it → NoRoute.
    assert!(matches!(resolve("GET", "/settlement/reportsXYZ"), RouteResolution::NoRoute));
    assert!(matches!(resolve("GET", "/portfolio/pnlXYZ"), RouteResolution::NoRoute));
    // `/settlement/reports/2026-01-01` under a GET-only prefix with POST → 405.
    assert!(matches!(resolve("POST", "/settlement/reports/2026-01-01"), RouteResolution::MethodNotAllowed));
}

#[test]
fn allowed_methods_list() {
    assert_eq!(RouteTable::allowed_methods("/settlement/reports"), Some(vec!["GET"]));
    assert_eq!(RouteTable::allowed_methods("/settlement/reports/2026-01-01"), Some(vec!["GET"]));
    assert_eq!(RouteTable::allowed_methods("/settlement/settle"), Some(vec!["POST"]));
    assert_eq!(RouteTable::allowed_methods("/portfolio/pnl"), Some(vec!["GET"]));
    assert_eq!(RouteTable::allowed_methods("/nope/nowhere"), None);
}

#[test]
fn rewrite_path_strips_gateway_prefix() {
    assert_eq!(
        RouteTable::rewrite_path(Upstream::Settlement, "/settlement/reports/2026-01-01"),
        "/reports/2026-01-01"
    );
    assert_eq!(RouteTable::rewrite_path(Upstream::Settlement, "/settlement/settle"), "/settle");
    assert_eq!(RouteTable::rewrite_path(Upstream::Settlement, "/settlement/finalize/2026-01-01"), "/finalize/2026-01-01");
    assert_eq!(RouteTable::rewrite_path(Upstream::Settlement, "/settlement/discrepancies"), "/discrepancies");
    assert_eq!(RouteTable::rewrite_path(Upstream::Portfolio, "/portfolio/pnl"), "/pnl");
    assert_eq!(RouteTable::rewrite_path(Upstream::Portfolio, "/portfolio/pnl/ES"), "/pnl/ES");
    assert_eq!(RouteTable::rewrite_path(Upstream::Portfolio, "/portfolio/var"), "/var");
    // The bare prefix becomes the upstream root.
    assert_eq!(RouteTable::rewrite_path(Upstream::Settlement, "/settlement"), "/");
    // A path not owned by the upstream is returned unchanged.
    assert_eq!(RouteTable::rewrite_path(Upstream::Portfolio, "/settlement/settle"), "/settlement/settle");
}

/// A handle-level 404 must not touch the transport at all.
#[test]
fn handle_404_before_auth() {
    let fake = FakeTransport::new(|c| Err(ApiError::upstream("nope", format!("call {} {}", c.method, c.path), true)));
    let gw = gateway(&fake);
    let (status, body) = gw.handle("GET", "/nope", "", None, None);
    assert_api_envelope(status, &body, 404, "API-404", false);
    assert_eq!(ctx(&body, "method").as_deref(), Some("GET"));
    assert_eq!(ctx(&body, "path").as_deref(), Some("/nope"));
    // No auth hop, no proxy hop: the transport was never consulted.
    assert!(fake.calls().is_empty(), "404 must not call any upstream, got {:?}", fake.calls());

    let s = gw.stats_snapshot();
    assert_eq!(s.requests_total, 1);
    assert_eq!(s.route_404, 1);
    assert_eq!(s.route_hits, 0);
    assert_eq!(s.auth_rejected, 0);
}

/// A handle-level 405 must carry the `allowed` method list and skip auth.
#[test]
fn handle_405_before_auth_with_allowed() {
    let fake = FakeTransport::new(|_c| Ok(OutboundResult { status: 200, body: Value::Null }));
    let gw = gateway(&fake);
    let (status, body) = gw.handle("DELETE", "/portfolio/pnl", "", None, None);
    assert_api_envelope(status, &body, 405, "API-405", false);
    assert_eq!(ctx(&body, "method").as_deref(), Some("DELETE"));
    assert_eq!(ctx(&body, "path").as_deref(), Some("/portfolio/pnl"));
    let allowed = body
        .get("error")
        .and_then(|e| e.get("context"))
        .and_then(|c| c.get("allowed"))
        .and_then(|a| a.as_array())
        .map(|a| a.iter().map(|v| v.as_str().unwrap_or("").to_string()).collect::<Vec<_>>());
    assert_eq!(allowed, Some(vec!["GET".to_string()]));

    // Even a perfectly good token cannot paper over a 405: the response is
    // produced before authentication.
    let fake2 = FakeTransport::new(|_c| Ok(OutboundResult { status: 200, body: s12_verify_ok("u", "j", "k", &["read", "write"], 9) }));
    let gw2 = gateway(&fake2);
    let (st2, b2) = gw2.handle("DELETE", "/portfolio/pnl", "", Some("Bearer whatever"), None);
    assert_eq!(st2, 405);
    assert_eq!(b2.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()), Some("API-405"));

    let s = gw.stats_snapshot();
    assert_eq!(s.requests_total, 1);
    assert_eq!(s.route_405, 1);
    assert_eq!(s.route_hits, 0);
    assert_eq!(s.proxied, 0);
}

/// 404 wins over 405 semantics: an unknown prefix under a known method is a
/// 404 (no `allowed` list).
#[test]
fn unknown_prefix_is_404_not_405() {
    let fake = FakeTransport::new(|_c| Ok(OutboundResult { status: 200, body: Value::Null }));
    let gw = gateway(&fake);
    let (status, body) = gw.handle("POST", "/settlement/unknown", "", Some("Bearer t"), None);
    assert_api_envelope(status, &body, 404, "API-404", false);
    assert!(body
        .get("error")
        .and_then(|e| e.get("context"))
        .and_then(|c| c.get("allowed"))
        .is_none(), "404 must not carry an `allowed` list");
    let s = gw.stats_snapshot();
    assert_eq!(s.route_404, 1);
    assert_eq!(s.route_405, 0);
}
