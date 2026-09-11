//! S12 HTTP end-to-end tests: `http::start` on an ephemeral port (port 0),
//! exercised through the template client `server::request`.

use authsvc::config::AuthConfig;
use authsvc::core::{AuthManager, ManualClock};
use authsvc::http;
use authsvc::json::Value;
#[allow(dead_code)]
mod common;

use common::*;
use std::sync::Arc;

fn obj(pairs: Vec<(&str, Value)>) -> Value {
    Value::object(pairs)
}

fn verify_req(token: &str, scope: Option<&str>) -> Value {
    let mut p = vec![("token", Value::from(token))];
    if let Some(s) = scope {
        p.push(("scope", Value::from(s)));
    }
    obj(p)
}

struct Live {
    port: u16,
    clock: Arc<ManualClock>,
    mgr: Arc<AuthManager>,
}

/// Boot the real server with the two known keys and a fixed manual clock.
fn boot() -> Live {
    let clock = Arc::new(ManualClock::new(T0));
    let clock_dyn: Arc<dyn authsvc::core::Clock> = clock.clone();
    let mgr = Arc::new(AuthManager::with_clock(
        AuthConfig::default(),
        vec![key(ALPHA_KID, ALPHA_SECRET, T0 - 2 * SECOND), key(BETA_KID, BETA_SECRET, T0 - SECOND)],
        clock_dyn,
    ));
    let handle = http::start("127.0.0.1", 0, Arc::clone(&mgr)).expect("server start failed");
    Live { port: handle.port(), clock, mgr }
}

#[test]
fn healthz_and_readyz() {
    let live = boot();
    let (st, b) = call(live.port, "GET", "/healthz", None);
    assert_eq!(st, 200);
    assert_eq!(b.get("status").and_then(|v| v.as_str()), Some("ok"));
    assert_eq!(b.get("service").and_then(|v| v.as_str()), Some("auth-service"));
    assert_eq!(b.get("version").and_then(|v| v.as_str()), Some("1.0.0"));

    let (st, b) = call(live.port, "GET", "/readyz", None);
    assert_eq!(st, 200);
    assert_eq!(b.get("status").and_then(|v| v.as_str()), Some("ready"));
    assert!(b.get("reasons").unwrap().as_array().unwrap().is_empty());
}

#[test]
fn token_then_verify_e2e() {
    let live = boot();
    let req = obj(vec![
        ("sub", Value::from("svc:api-gateway")),
        ("scopes", Value::from(vec![Value::from("orders:read"), Value::from("orders:cancel")])),
        ("ttl_ns", Value::from(3_600_000_000_000i64)),
    ]);
    let (st, resp) = call(live.port, "POST", "/token", Some(&req));
    assert_eq!(st, 200);
    let token = resp.get("token").unwrap().as_str().unwrap().to_string();
    let jti = resp.get("jti").unwrap().as_str().unwrap().to_string();
    assert_eq!(resp.get("kid").and_then(|v| v.as_str()), Some(BETA_KID));
    assert_eq!(resp.get("sub").and_then(|v| v.as_str()), Some("svc:api-gateway"));
    assert_eq!(resp.get("iat").and_then(|v| v.as_i64()), Some(T0));
    assert_eq!(resp.get("exp").and_then(|v| v.as_i64()), Some(T0 + 3_600_000_000_000));
    assert_eq!(resp.get("scope").unwrap().as_array().unwrap().len(), 2);
    assert_eq!(token.matches('.').count(), 2);

    // Verify with a required scope the token has.
    let (st, v) = call(live.port, "POST", "/verify", Some(&verify_req(&token, Some("orders:cancel"))));
    assert_eq!(st, 200);
    assert_eq!(v.get("valid").and_then(|x| x.as_bool()), Some(true));
    assert_eq!(v.get("sub").and_then(|x| x.as_str()), Some("svc:api-gateway"));
    assert_eq!(v.get("jti").and_then(|x| x.as_str()), Some(jti.as_str()));
    assert_eq!(v.get("kid").and_then(|x| x.as_str()), Some(BETA_KID));
    assert_eq!(v.get("exp").and_then(|x| x.as_i64()), Some(T0 + 3_600_000_000_000));

    // Same token, required scope it does NOT have -> 400 AUT-211.
    let (st, v) = call(live.port, "POST", "/verify", Some(&verify_req(&token, Some("portfolio:write"))));
    assert_eq!(st, 400);
    assert_err(&v, "AUT-211");
}

#[test]
fn verify_missing_and_garbage_token() {
    let live = boot();
    // Missing token field entirely.
    let (st, b) = call(live.port, "POST", "/verify", Some(&Value::object(vec![])));
    assert_eq!(st, 400);
    assert_err(&b, "AUT-214");
    // token present but wrong type.
    let (st, b) = call(live.port, "POST", "/verify", Some(&obj(vec![("token", Value::from(123u64))])));
    assert_eq!(st, 400);
    assert_err(&b, "AUT-214");
    // Not a compact JWT at all.
    let (st, b) = call(live.port, "POST", "/verify", Some(&obj(vec![("token", Value::from("junk"))])));
    assert_eq!(st, 400);
    assert_err(&b, "AUT-204");
    assert_eq!(live.mgr.stats().verified_fail, 1, "only 'junk' reached verify()");
}

#[test]
fn token_400_paths() {
    let live = boot();
    // Missing / empty / non-string sub.
    expect_code(live.port, "POST", "/token", Some(&Value::object(vec![])), "AUT-202");
    expect_code(live.port, "POST", "/token", Some(&obj(vec![("sub", Value::from(""))])), "AUT-202");
    expect_code(live.port, "POST", "/token", Some(&obj(vec![("sub", Value::from(42u64))])), "AUT-202");
    // scopes: not an array / non-string entry / empty entry.
    expect_code(live.port, "POST", "/token", Some(&obj(vec![("sub", Value::from("a")), ("scopes", Value::from("read"))])), "AUT-201");
    expect_code(live.port, "POST", "/token", Some(&obj(vec![("sub", Value::from("a")), ("scopes", Value::from(vec![Value::from("ok"), Value::from(7u64)]))])), "AUT-201");
    expect_code(live.port, "POST", "/token", Some(&obj(vec![("sub", Value::from("a")), ("scopes", Value::from(vec![Value::from("   ")]))])), "AUT-201");
    // ttl_ns: non-integer type, negative value.
    expect_code(live.port, "POST", "/token", Some(&obj(vec![("sub", Value::from("a")), ("ttl_ns", Value::from("60"))])), "AUT-203");
    expect_code(live.port, "POST", "/token", Some(&obj(vec![("sub", Value::from("a")), ("ttl_ns", Value::from(-5i64))])), "AUT-203");
    // None of the above minted a token.
    assert_eq!(live.mgr.stats().issued, 0);
}

#[test]
fn revoke_e2e_and_400s() {
    let live = boot();
    let (st, resp) = call(live.port, "POST", "/token", Some(&obj(vec![("sub", Value::from("s"))])));
    assert_eq!(st, 200);
    let jti = resp.get("jti").unwrap().as_str().unwrap().to_string();
    let token = resp.get("token").unwrap().as_str().unwrap().to_string();

    let rreq = obj(vec![("jti", Value::from(jti.as_str()))]);
    let (st, r) = call(live.port, "POST", "/revoke", Some(&rreq));
    assert_eq!(st, 200);
    assert_eq!(r.get("revoked").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(r.get("created").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(r.get("jti").and_then(|v| v.as_str()), Some(jti.as_str()));

    // Idempotent second call.
    let (st, r) = call(live.port, "POST", "/revoke", Some(&rreq));
    assert_eq!(st, 200);
    assert_eq!(r.get("created").and_then(|v| v.as_bool()), Some(false));

    // The token is now dead.
    let (st, v) = call(live.port, "POST", "/verify", Some(&verify_req(&token, None)));
    assert_eq!(st, 400);
    assert_err(&v, "AUT-210");

    // Missing / empty jti.
    expect_code(live.port, "POST", "/revoke", Some(&Value::object(vec![])), "AUT-213");
    expect_code(live.port, "POST", "/revoke", Some(&obj(vec![("jti", Value::from(""))])), "AUT-213");
    assert_eq!(live.mgr.stats().revoked, 1);
}

#[test]
fn keys_endpoint_never_exposes_secret() {
    let live = boot();
    let (st, b) = call(live.port, "GET", "/keys", None);
    assert_eq!(st, 200);
    let arr = b.as_array().unwrap();
    assert_eq!(arr.len(), 2, "one entry per key in the ring");
    let kids: Vec<&str> = arr.iter().map(|k| k.get("kid").unwrap().as_str().unwrap()).collect();
    assert!(kids.contains(&ALPHA_KID));
    assert!(kids.contains(&BETA_KID));
    let mut fps = Vec::new();
    for k in arr {
        assert_eq!(k.get("alg").and_then(|v| v.as_str()), Some("HS256"));
        assert!(k.get("created_ns").and_then(|v| v.as_i64()).is_some());
        assert!(k.get("active").and_then(|v| v.as_bool()).is_some());
        let fp = k.get("fingerprint").unwrap().as_str().unwrap();
        assert_eq!(fp.len(), 16, "fingerprint is 16 hex chars");
        assert!(fp.chars().all(|c| c.is_ascii_hexdigit()));
        fps.push(fp);
    }
    assert_ne!(fps[0], fps[1], "distinct keys -> distinct fingerprints");

    // No secret material anywhere in the serialized response.
    let blob = b.to_json().to_lowercase();
    assert!(!blob.contains("alpha-secret-0123456789"));
    assert!(!blob.contains("beta-secret-0123456789"));
    assert!(!blob.contains("secret"));
}

#[test]
fn stats_counts_and_structured_404_405() {
    let live = boot();
    let (st, resp) = call(live.port, "POST", "/token", Some(&obj(vec![("sub", Value::from("s"))])));
    assert_eq!(st, 200);
    let token = resp.get("token").unwrap().as_str().unwrap().to_string();
    let jti = resp.get("jti").unwrap().as_str().unwrap().to_string();
    let (st, _) = call(live.port, "POST", "/verify", Some(&verify_req(&token, None)));
    assert_eq!(st, 200);
    call(live.port, "POST", "/verify", Some(&obj(vec![("token", Value::from("nope"))]))); // verified_fail += 1
    let (st, _) = call(live.port, "POST", "/revoke", Some(&obj(vec![("jti", Value::from(jti.as_str()))])));
    assert_eq!(st, 200);

    let (st, s) = call(live.port, "GET", "/stats", None);
    assert_eq!(st, 200);
    assert_eq!(s.get("issued").and_then(|v| v.as_i64()), Some(1));
    assert_eq!(s.get("verified_ok").and_then(|v| v.as_i64()), Some(1));
    assert_eq!(s.get("verified_fail").and_then(|v| v.as_i64()), Some(1));
    assert_eq!(s.get("revoked").and_then(|v| v.as_i64()), Some(1));
    assert_eq!(s.get("rotations").and_then(|v| v.as_i64()), Some(0));

    // Structured 404: no route carries method+path context (code AUT-002).
    let (st, b) = call(live.port, "GET", "/does/not/exist", None);
    assert_eq!(st, 404);
    assert_err(&b, "AUT-002");
    let ctx = b.get("error").unwrap().get("context").unwrap();
    assert_eq!(ctx.get("method").and_then(|v| v.as_str()), Some("GET"));
    assert_eq!(ctx.get("path").and_then(|v| v.as_str()), Some("/does/not/exist"));

    // 405 on a known path with the wrong method, listing allowed methods.
    let (st, b) = call(live.port, "DELETE", "/token", None);
    assert_eq!(st, 405);
    assert_err(&b, "AUT-003");
    let allowed = b.get("error").unwrap().get("context").unwrap().get("allowed").unwrap().as_array().unwrap();
    let list: Vec<&str> = allowed.iter().map(|v| v.as_str().unwrap()).collect();
    assert!(list.contains(&"POST"));
}

#[test]
fn expiry_advances_through_the_shared_clock() {
    let live = boot();
    let (st, resp) = call(
        live.port, "POST", "/token",
        Some(&obj(vec![("sub", Value::from("s")), ("ttl_ns", Value::from(10 * SECOND))])),
    );
    assert_eq!(st, 200);
    let token = resp.get("token").unwrap().as_str().unwrap().to_string();
    let (st, _) = call(live.port, "POST", "/verify", Some(&verify_req(&token, None)));
    assert_eq!(st, 200, "fresh token verifies over HTTP");

    // The server shares this manual clock: advance past exp + 5s skew.
    live.clock.advance(10 * SECOND + 5 * SECOND + 1);
    let (st, v) = call(live.port, "POST", "/verify", Some(&verify_req(&token, None)));
    assert_eq!(st, 400);
    assert_err(&v, "AUT-208");
}
