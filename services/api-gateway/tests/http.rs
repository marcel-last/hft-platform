//! End-to-end tests: the real HTTP listener (`http::start`) on a `127.0.0.1`
//! ephemeral port, driven by `raw_request` (the template client cannot attach
//! the `Authorization` header). S12 and the upstreams are still faked through
//! the injected `Transport`, so no other service needs to be running.

mod common;

use std::sync::Arc;

use apigw::core::OutboundResult;
use apigw::errors::ApiError;
use apigw::http;
use apigw::json::Value;
use common::*;

const EXP: i64 = 1_788_000_000_000_000_000;

/// S12 (7720) → valid token with `scopes`; every other upstream → `up_status`
/// + `up_body`. The returned `(handle, fake)` drives one listener.
fn boot(scopes: &[&str], up_status: u16, up_body: Value) -> (apigw::server::ServerHandle, Arc<FakeTransport>) {
    let owned: Vec<String> = scopes.iter().map(|s| s.to_string()).collect();
    let fake = FakeTransport::new(move |c| {
        if c.addr.1 == 7720 {
            let refs: Vec<&str> = owned.iter().map(|s| s.as_str()).collect();
            Ok(OutboundResult { status: 200, body: s12_verify_ok("trader", "jti-1", "kid-alpha", &refs, EXP) })
        } else {
            Ok(OutboundResult { status: up_status, body: up_body.clone() })
        }
    });
    let gw = gateway(&fake);
    let handle = http::start("127.0.0.1", 0, gw).expect("bind ephemeral port");
    (handle, fake)
}

fn host_port(h: &apigw::server::ServerHandle) -> (String, u16) {
    ("127.0.0.1".to_string(), h.port())
}

#[test]
fn healthz_and_readyz_with_s12_reachable() {
    let (h, _fake) = boot(&["read", "write"], 200, Value::Null);
    let (host, port) = host_port(&h);

    let (st, body) = raw_get(&host, port, "/healthz").expect("healthz");
    assert_eq!(st, 200);
    assert_eq!(field_str(&body, "status").as_deref(), Some("ok"));
    assert_eq!(field_str(&body, "service").as_deref(), Some("api-gateway"));
    assert_eq!(field_str(&body, "version").as_deref(), Some("1.0.0"));

    // readyz checks S12 reachability → ready.
    let (st, body) = raw_get(&host, port, "/readyz").expect("readyz");
    assert_eq!(st, 200);
    assert_eq!(field_str(&body, "status").as_deref(), Some("ready"));
    assert_eq!(body.get("reasons").and_then(|r| r.as_array()).map(|a| a.len()), Some(0));
}

#[test]
fn readyz_not_ready_when_s12_down() {
    // S12 unreachable → readyz reports not_ready (a single reason), while
    // healthz stays ok (liveness is independent of readiness).
    let fake = FakeTransport::new(move |c| {
        if c.addr.1 == 7720 {
            Err(ApiError::upstream("auth-service", "unreachable: Connection refused", true))
        } else {
            Ok(OutboundResult { status: 200, body: Value::Null })
        }
    });
    let gw = gateway(&fake);
    let h = http::start("127.0.0.1", 0, gw).expect("bind");
    let (host, port) = host_port(&h);

    let (st, body) = raw_get(&host, port, "/healthz").expect("healthz");
    assert_eq!(st, 200);
    assert_eq!(field_str(&body, "status").as_deref(), Some("ok"));

    let (st, body) = raw_get(&host, port, "/readyz").expect("readyz");
    assert_eq!(st, 200);
    assert_eq!(field_str(&body, "status").as_deref(), Some("not_ready"));
    let reasons = body.get("reasons").and_then(|r| r.as_array()).map(|a| a.len()).unwrap_or(0);
    assert_eq!(reasons, 1, "exactly one not_ready reason");
}

#[test]
fn authed_round_trip_proxies_and_passes_through() {
    let payload = Value::object(vec![("symbol", "ES".into()), ("pnl", 10.5f64.into())]);
    let (h, fake) = boot(&["read", "write"], 200, payload);
    let (host, port) = host_port(&h);

    // No Authorization → 401 API-201, and S12 is never called.
    let (st, body) = raw_request(&host, port, "GET", "/portfolio/pnl", None, None, 2000).expect("no-auth");
    assert_api_envelope(st, &body, 401, "API-201", false);
    assert!(fake.to_port(7720).is_empty(), "no token → no S12 call");

    // With a bearer → verify (S12) then proxy (S9) → upstream body verbatim.
    let (st, body) = raw_request(&host, port, "GET", "/portfolio/pnl/ES", None, Some("tok-123"), 2000).expect("authed");
    assert_eq!(st, 200, "authed GET must proxy (body {})", body.to_json());
    assert_eq!(body.get("symbol").and_then(|v| v.as_str()), Some("ES"));

    // Exactly one verify hop with the bearer, one proxy hop with rewritten path.
    let verify = fake.to_port(7720);
    assert_eq!(verify.len(), 1);
    assert_eq!(verify[0].method, "POST");
    assert_eq!(verify[0].path, "/verify");
    assert_eq!(verify[0].body.as_ref().and_then(|b| b.get("token")).and_then(|t| t.as_str()), Some("tok-123"));
    let up = fake.to_port(7690);
    assert_eq!(up.len(), 1);
    assert_eq!(up[0].path, "/pnl/ES");
}

#[test]
fn bad_json_body_is_api204_before_proxy() {
    // A malformed body is rejected at the HTTP layer as 400 API-204 before the
    // pipeline proxies anything.
    let (h, _fake) = boot(&["read", "write"], 200, Value::Null);
    let (host, port) = host_port(&h);
    let raw = TcpWriter::new();
    let (st, body) = raw.post_bad_json(&host, port, "/settlement/settle", "Bearer t", "{not json", 2000).expect("bad json");
    assert_api_envelope(st, &body, 400, "API-204", false);
}

#[test]
fn structured_404_and_405_over_http() {
    let (h, _fake) = boot(&["read", "write"], 200, Value::Null);
    let (host, port) = host_port(&h);

    // Unknown upstream prefix → 404 API-404 with {method,path}.
    let (st, body) = raw_request(&host, port, "GET", "/unknown/thing", None, Some("tok"), 2000).expect("404");
    assert_api_envelope(st, &body, 404, "API-404", false);
    assert_eq!(ctx(&body, "method").as_deref(), Some("GET"));
    assert_eq!(ctx(&body, "path").as_deref(), Some("/unknown/thing"));

    // Known prefix, wrong method → 405 API-405 with the allowed list.
    let (st, body) = raw_request(&host, port, "DELETE", "/portfolio/pnl", None, Some("tok"), 2000).expect("405");
    assert_api_envelope(st, &body, 405, "API-405", false);
    let allowed = body
        .get("error").and_then(|e| e.get("context")).and_then(|c| c.get("allowed"))
        .and_then(|a| a.as_array())
        .map(|a| a.iter().map(|v| v.as_str().unwrap_or("").to_string()).collect::<Vec<_>>());
    assert_eq!(allowed, Some(vec!["GET".to_string()]));

    // A local endpoint hit with a wrong method → 405 too (served by the local
    // router, not the proxy).
    let (st, body) = raw_request(&host, port, "POST", "/stats", None, None, 2000).expect("local 405");
    assert_eq!(st, 405);
    assert_eq!(body.get("error").and_then(|e| e.get("code")).and_then(|c| c.as_str()), Some("API-405"));
}

#[test]
fn stats_reflects_traffic() {
    let (h, _fake) = boot(&["read", "write"], 200, Value::object(vec![("k", "v".into())]));
    let (host, port) = host_port(&h);

    // Baseline (this listener has served nothing yet).
    let (st, base) = raw_get(&host, port, "/stats").expect("stats baseline");
    assert_eq!(st, 200);
    let baseline = base.get("requests_total").and_then(|v| v.as_i64()).unwrap_or(0);

    // A 404 and an authed success, then re-read.
    let _ = raw_request(&host, port, "GET", "/missing", None, Some("tok"), 2000);
    let _ = raw_request(&host, port, "GET", "/portfolio/pnl", None, Some("tok"), 2000);

    let (st, body) = raw_get(&host, port, "/stats").expect("stats after");
    assert_eq!(st, 200);
    let after = body.get("requests_total").and_then(|v| v.as_i64()).unwrap_or(0);
    assert_eq!(after, baseline + 2, "two proxied-path requests counted");
    let r404 = body.get("route_404").and_then(|v| v.as_i64()).unwrap_or(0);
    let proxied = body.get("proxied").and_then(|v| v.as_i64()).unwrap_or(0);
    assert!(r404 >= 1, "the 404 was counted");
    assert!(proxied >= 1, "the authed proxy was counted");
}

/// Minimal helper to POST a raw (possibly invalid) JSON body with a bearer.
struct TcpWriter;
impl TcpWriter {
    fn new() -> Self { TcpWriter }
    fn post_bad_json(
        &self, host: &str, port: u16, path: &str, bearer: &str, raw_body: &str, timeout_ms: u64,
    ) -> std::io::Result<(u16, Value)> {
        let (st, body) = raw_request_raw(host, port, "POST", path, raw_body, Some(bearer), timeout_ms)?;
        Ok((st, body))
    }
}
