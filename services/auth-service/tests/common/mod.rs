//! Shared fixtures for the auth-service integration tests.
//!
//! Everything here is service-level: config builders, a `ManualClock` harness,
//! two known signing keys, a small raw-token crafter, and a thin wrapper over
//! the template HTTP client. The template primitives (SHA-256 / HMAC /
//! base64url / JSON) self-verify under `cargo test` — nothing in this crate
//! re-tests them.

use authsvc::config::AuthConfig;
use authsvc::core::{AuthManager, ManualClock};
use authsvc::errors::AuthError;
use authsvc::json::Value;
use authsvc::models::{Claims, IssuedToken, SigningKey};
use authsvc::server;
use std::sync::Arc;

/// Fixed "now" for every clock-based test: 2026-01-01T00:00:00Z.
pub const T0: i64 = 1_767_225_600_000_000_000;

/// One second in nanoseconds.
pub const SECOND: i64 = 1_000_000_000;

/// Known key material — deterministic signatures, asserted in `GET /keys` tests.
pub const ALPHA_KID: &str = "kid-alpha";
pub const ALPHA_SECRET: &[u8] = b"alpha-secret-0123456789";
pub const BETA_KID: &str = "kid-beta";
pub const BETA_SECRET: &[u8] = b"beta-secret-0123456789";

/// A signing key with fixed material (active, created at `created_ns`).
pub fn key(kid: &str, secret: &[u8], created_ns: i64) -> SigningKey {
    SigningKey::new(kid, secret.to_vec(), created_ns)
}

/// Explicit-knob config builder for the token / key namespaces.
pub fn config(
    ttl_ns: i64,
    skew_ns: i64,
    max_ttl_ns: i64,
    max_revocations: usize,
    ring_size: usize,
) -> AuthConfig {
    let mut c = AuthConfig::default();
    c.token.ttl_ns = ttl_ns;
    c.token.clock_skew_ns = skew_ns;
    c.token.max_ttl_ns = max_ttl_ns;
    c.token.max_revocations = max_revocations;
    c.keys.ring_size = ring_size;
    c
}

/// Test harness: manager + clock + the two known keys, all fixed at `T0`.
pub struct Harness {
    pub cfg: AuthConfig,
    pub clock: Arc<ManualClock>,
    pub mgr: AuthManager,
    pub alpha: SigningKey,
    pub beta: SigningKey,
}

/// Standard harness: default config, alpha (created T0) and beta (created
/// T0+1) — beta is the newest active key, so `issue()` signs with beta.
pub fn harness() -> Harness {
    harness_cfg(AuthConfig::default())
}

/// Harness with a custom config and the same two known keys. Both keys are
/// created *before* the clock starts at T0 so a rotated key (stamped at the
/// current clock instant) is always newer than either fixture key.
pub fn harness_cfg(cfg: AuthConfig) -> Harness {
    let clock = Arc::new(ManualClock::new(T0));
    let alpha = key(ALPHA_KID, ALPHA_SECRET, T0 - 2 * SECOND);
    let beta = key(BETA_KID, BETA_SECRET, T0 - SECOND);
    let clock_dyn: Arc<dyn authsvc::core::Clock> = clock.clone();
    let mgr = AuthManager::with_clock(cfg, vec![alpha.clone(), beta.clone()], clock_dyn);
    Harness { cfg, clock, mgr, alpha, beta }
}

/// Issue a token through the harness; panics on failure (all call sites
/// expect success unless stated otherwise).
pub fn issue(h: &Harness, sub: &str, scopes: &[String], ttl: Option<i64>) -> IssuedToken {
    h.mgr.issue(sub, scopes, ttl).expect("issue failed")
}

/// Verify a token expecting failure; returns the error for code assertions.
pub fn verify_err(mgr: &AuthManager, token: &str, scope: Option<&str>) -> AuthError {
    match mgr.verify(token, scope) {
        Ok(_) => panic!("expected verify failure"),
        Err(e) => e,
    }
}

/// Fresh default claims valid from `now` for `ttl_ns`.
pub fn claims_at(now: i64, ttl_ns: i64, sub: &str, scope: Vec<String>) -> Claims {
    Claims {
        iss: "auth-service".to_string(),
        sub: sub.to_string(),
        aud: None,
        jti: format!("jti-raw-{}", sub),
        iat: now,
        nbf: now,
        exp: now + ttl_ns,
        scope,
    }
}

/// Craft a compact JWT signed directly by `key` (bypasses `issue`) — needed
/// for wrong-key, retired-key and not-yet-valid scenarios.
pub fn raw_token(key: &SigningKey, c: &Claims) -> String {
    let header = Value::object(vec![
        ("alg", Value::from("HS256")),
        ("typ", Value::from("JWT")),
        ("kid", Value::from(key.kid.as_str())),
    ]);
    let h64 = authsvc::crypto::base64url_encode(header.to_json().as_bytes());
    let p64 = authsvc::crypto::base64url_encode(c.to_json().to_json().as_bytes());
    let sig = key.sign(&format!("{}.{}", h64, p64));
    format!("{}.{}.{}", h64, p64, authsvc::crypto::base64url_encode(&sig))
}

/// HTTP round-trip against a server bound to an ephemeral port.
pub fn call(port: u16, method: &str, path: &str, body: Option<&Value>) -> (u16, Value) {
    server::request(("127.0.0.1", port), method, path, body, 2000).expect("http request failed")
}

/// Assert `body` is the CONVENTIONS §1.2 error envelope with `code`.
pub fn assert_err(body: &Value, code: &str) {
    let e = body
        .get("error")
        .unwrap_or_else(|| panic!("expected error object, got: {}", body.to_json()));
    assert_eq!(
        e.get("code").and_then(|v| v.as_str()),
        Some(code),
        "envelope: {}",
        body.to_json()
    );
    assert_eq!(e.get("service").and_then(|v| v.as_str()), Some("auth-service"));
    assert!(e.get("message").and_then(|v| v.as_str()).is_some(), "missing message");
    assert!(e.get("retryable").and_then(|v| v.as_bool()).is_some(), "missing retryable");
    assert!(e.get("context").is_some(), "missing context");
}

/// Call and assert a 4xx+ standard envelope with `code`; returns (status, body).
pub fn expect_code(port: u16, method: &str, path: &str, body: Option<&Value>, code: &str) -> (u16, Value) {
    let (status, resp) = call(port, method, path, body);
    assert!(status >= 400, "expected >=400 for {}, got {}", code, status);
    assert_err(&resp, code);
    (status, resp)
}
