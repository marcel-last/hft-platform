//! Token verification + scope authorization through `Gateway`.
//!
//! The canned S12 responses are FAITHFUL to the real S12 wire (see
//! `common::s12_verify_ok` / `s12_reject`): a 2xx success is a flat claims
//! object, a 4xx rejection carries `error.code` (+ optional `context.reason`).
//! The transport is inspected to assert the gateway actually POSTed the token
//! to S12 `/verify` with the right body.

mod common;

use std::sync::Arc;

use apigw::core::OutboundResult;
use apigw::errors::ApiError;
use apigw::json::Value;
use apigw::models::parse_bearer;
use common::*;

const EXP: i64 = 1_788_000_000_000_000_000; // arbitrary future ns, S12 owns expiry

/// A gateway whose S12 hop (port 7720) returns a valid token and whose
/// upstream hop (any other port) returns `{"symbol":"ES"}`.
fn ok_gateway() -> (apigw::core::Gateway, Arc<FakeTransport>) {
    let fake = FakeTransport::new(|c| {
        if c.addr.1 == 7720 {
            Ok(OutboundResult { status: 200, body: s12_verify_ok("trader-1", "jti-1", "kid-alpha", &["read", "write"], EXP) })
        } else {
            Ok(OutboundResult { status: 200, body: Value::object(vec![("symbol", "ES".into())]) })
        }
    });
    (gateway(&fake), fake)
}

#[test]
fn missing_token_is_401_api201() {
    let (gw, fake) = ok_gateway();
    let (status, body) = gw.handle("GET", "/portfolio/pnl", "", None, None);
    assert_api_envelope(status, &body, 401, "API-201", false);
    // No auth hop may be made: the header was never parsed to a token.
    assert!(fake.calls().is_empty(), "missing token must not call S12, got {:?}", fake.calls());
    let s = gw.stats_snapshot();
    assert_eq!(s.auth_rejected, 1);
    assert_eq!(s.auth_ok, 0);
}

#[test]
fn malformed_bearer_is_401_api201() {
    let (gw, fake) = ok_gateway();
    for hdr in ["Bearer", "bearer", "Token abc", "Bearer  ", "Basic Zm9v"] {
        let (status, body) = gw.handle("GET", "/portfolio/pnl", "", Some(hdr), None);
        assert_eq!(status, 401, "header {:?} must be 401", hdr);
        assert_eq!(body.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()), Some("API-201"));
    }
    assert!(fake.calls().is_empty(), "malformed bearer must not call S12");
}

#[test]
fn valid_token_round_trip_to_s12() {
    let (gw, fake) = ok_gateway();
    let (status, body) = gw.handle("GET", "/portfolio/pnl", "", Some("Bearer tok-abc"), None);
    assert_eq!(status, 200, "valid token must proxy, got {} ({})", status, body.to_json());
    assert_eq!(body.get("symbol").and_then(|v| v.as_str()), Some("ES"));

    // Exactly one S12 call, with the right method/path/body.
    let calls = fake.to_port(7720);
    assert_eq!(calls.len(), 1, "exactly one S12 /verify hop");
    assert_eq!(calls[0].method, "POST");
    assert_eq!(calls[0].path, "/verify");
    let sent_tok = calls[0]
        .body
        .as_ref()
        .and_then(|b| b.get("token"))
        .and_then(|t| t.as_str())
        .unwrap_or("");
    assert_eq!(sent_tok, "tok-abc");
    let s = gw.stats_snapshot();
    assert_eq!(s.auth_ok, 1);
    assert_eq!(s.proxied, 1);
}

#[test]
fn s12_4xx_becomes_token_rejected_api202() {
    let fake = FakeTransport::new(|_c| Ok(OutboundResult { status: 400, body: s12_reject("AUT-208", Some("token is past its exp")) }));
    let gw = gateway(&fake);
    let (status, body) = gw.handle("GET", "/portfolio/pnl", "", Some("Bearer expired-tok"), None);
    assert_api_envelope(status, &body, 401, "API-202", false);
    // The detail must carry the S12 code AND reason.
    assert_eq!(ctx(&body, "detail").as_deref(), Some("AUT-208: token is past its exp"));
    let s = gw.stats_snapshot();
    assert_eq!(s.auth_rejected, 1);
    assert_eq!(s.auth_ok, 0);
}

#[test]
fn s12_4xx_without_reason_is_still_api202() {
    // Real S12 sometimes carries only the code (no `reason` context).
    let fake = FakeTransport::new(|_c| Ok(OutboundResult { status: 400, body: s12_reject("AUT-207", None) }));
    let gw = gateway(&fake);
    let (status, body) = gw.handle("GET", "/portfolio/pnl", "", Some("Bearer tampered"), None);
    assert_api_envelope(status, &body, 401, "API-202", false);
    assert_eq!(ctx(&body, "detail").as_deref(), Some("AUT-207"));
}

#[test]
fn s12_5xx_is_auth_upstream_api503() {
    let fake = FakeTransport::new(|_c| Ok(OutboundResult { status: 500, body: Value::object(vec![("error", Value::object(vec![("code", "AUT-100".into())]))]) }));
    let gw = gateway(&fake);
    let (status, body) = gw.handle("GET", "/portfolio/pnl", "", Some("Bearer t"), None);
    assert_api_envelope(status, &body, 503, "API-503", true);
    assert_eq!(ctx(&body, "detail").as_deref(), Some("auth-service answered 500"));
    let s = gw.stats_snapshot();
    assert_eq!(s.auth_upstream_errors, 1);
}

#[test]
fn s12_transport_failure_is_auth_upstream_api503() {
    // The design contract: a transport failure to S12 is API-503 AuthUpstream
    // (retryable), NOT the proxy's API-502 upstream error.
    let fake = FakeTransport::new(|_c| Err(ApiError::upstream("auth-service", "unreachable: Connection refused", true)));
    let gw = gateway(&fake);
    let (status, body) = gw.handle("GET", "/portfolio/pnl", "", Some("Bearer t"), None);
    assert_api_envelope(status, &body, 503, "API-503", true);
    let s = gw.stats_snapshot();
    assert_eq!(s.auth_upstream_errors, 1);
}

#[test]
fn s12_malformed_success_body_is_api503() {
    // 2xx but not a usable claims body → retryable upstream problem, not 200.
    let fake = FakeTransport::new(|_c| Ok(OutboundResult { status: 200, body: Value::object(vec![("unrelated", true.into())]) }));
    let gw = gateway(&fake);
    let (status, body) = gw.handle("GET", "/portfolio/pnl", "", Some("Bearer t"), None);
    assert_api_envelope(status, &body, 503, "API-503", true);
}

#[test]
fn scope_denied_is_403_api203_with_required() {
    // Read-only token on a write route.
    let fake = FakeTransport::new(|_c| Ok(OutboundResult { status: 200, body: s12_verify_ok("ro-user", "jti-ro", "kid-alpha", &["read"], EXP) }));
    let gw = gateway(&fake);
    let (status, body) = gw.handle("POST", "/settlement/settle", "", Some("Bearer ro-tok"), Some(Value::object(vec![("date", "2026-01-01".into())])));
    assert_api_envelope(status, &body, 403, "API-203", false);
    assert_eq!(ctx(&body, "required_scope").as_deref(), Some("write"));
    // No proxy hop: rejected before forwarding.
    assert!(fake.to_port(7740).is_empty(), "403 must not proxy");
    let s = gw.stats_snapshot();
    assert_eq!(s.auth_ok, 1);
    assert_eq!(s.scope_denied, 1);
    assert_eq!(s.proxied, 0);
}

#[test]
fn read_token_allowed_on_read_route() {
    // S12 (7720) answers verify; S9 (7690) answers the proxied GET.
    let fake = FakeTransport::new(|c| {
        if c.addr.1 == 7720 {
            Ok(OutboundResult { status: 200, body: s12_verify_ok("ro", "j", "k", &["read"], EXP) })
        } else {
            Ok(OutboundResult { status: 200, body: Value::object(vec![("total", 42i64.into())]) })
        }
    });
    let gw = gateway(&fake);
    let (status, body) = gw.handle("GET", "/portfolio/pnl", "", Some("Bearer ro-tok"), None);
    assert_eq!(status, 200);
    assert_eq!(body.get("total").and_then(|v| v.as_i64()), Some(42));
    let s = gw.stats_snapshot();
    assert_eq!(s.scope_denied, 0);
    assert_eq!(s.proxied, 1);
}

#[test]
fn parse_bearer_edge_cases() {
    assert_eq!(parse_bearer(Some("Bearer abc.def.ghi")).as_deref(), Some("abc.def.ghi"));
    assert_eq!(parse_bearer(Some("bearer  abc ")).as_deref(), Some("abc"));
    assert_eq!(parse_bearer(Some("Bearer   lots  of  space  tok")).as_deref(), Some("lots  of  space  tok"));
    assert_eq!(parse_bearer(None), None);
    assert_eq!(parse_bearer(Some("")), None);
    assert_eq!(parse_bearer(Some("Bearer")), None);
    assert_eq!(parse_bearer(Some("Bearer ")), None);
    assert_eq!(parse_bearer(Some("Token xyz")), None);
    assert_eq!(parse_bearer(Some("Basic Zm9v")), None);
    // The token itself is never re-trimmed past its own edges.
    assert_eq!(parse_bearer(Some("Bearer  a b")).as_deref(), Some("a b"));
}
