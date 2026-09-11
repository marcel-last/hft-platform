//! S12 token tests: issue/verify round-trip, tamper detection, wrong-key
//! rejection, time windows (ManualClock only), TTL capping, scope checks.

use authsvc::config::AuthConfig;
use authsvc::errors::AuthError;
use authsvc::json::Value;
use authsvc::models::{Claims, SigningKey};
#[allow(dead_code)]
mod common;

use common::*;

#[test]
fn issue_verify_round_trip() {
    let h = harness();
    let tok = issue(&h, "svc:execution-gateway", &["read".to_string(), "trade".to_string()], None);
    assert_eq!(tok.sub, "svc:execution-gateway");
    assert_eq!(tok.kid, BETA_KID, "newest active key (beta) signs");
    assert_eq!(tok.scope, vec!["read".to_string(), "trade".to_string()]);
    assert_eq!(tok.iat, T0);
    assert_eq!(tok.nbf, T0);
    assert_eq!(tok.exp, T0 + h.cfg.token.ttl_ns, "default ttl applied");
    assert_eq!(tok.token.matches('.').count(), 2, "compact 3-segment JWT");

    let vc = h.mgr.verify(&tok.token, None).expect("valid token rejected");
    assert_eq!(vc.claims.sub, "svc:execution-gateway");
    assert_eq!(vc.claims.iss, "auth-service");
    assert_eq!(vc.claims.scope, tok.scope);
    assert_eq!(vc.kid, BETA_KID);
    assert_eq!(h.mgr.stats().issued, 1);
    assert_eq!(h.mgr.stats().verified_ok, 1);
    assert_eq!(h.mgr.stats().verified_fail, 0);
}

#[test]
fn tampered_payload_and_header_fail_bad_signature() {
    let h = harness();
    let tok = issue(&h, "sub-a", &["read".to_string()], None);
    let segs: Vec<&str> = tok.token.splitn(3, '.').collect();
    let (h64, p64, sig) = (segs[0], segs[1], segs[2]);
    let payload = authsvc::json::parse(std::str::from_utf8(&authsvc::crypto::base64url_decode(p64).unwrap()).unwrap()).unwrap();

    // Swap the subject in the payload, keep the old signature.
    let mut forged = payload;
    forged.set("sub", Value::from("sub-evil"));
    let forged_p64 = authsvc::crypto::base64url_encode(forged.to_json().as_bytes());
    let e = verify_err(&h.mgr, &format!("{}.{}.{}", h64, forged_p64, sig), None);
    assert_eq!(e.code, "AUT-207", "tampered payload -> BadSignature");
    assert!(!e.retryable);

    // Corrupt one character of the signature segment: structure still parses
    // (valid base64url) so the failure must be the HMAC check itself.
    let c0 = sig.chars().next().unwrap();
    let bad_sig = if c0 == 'A' { format!("B{}", &sig[1..]) } else { format!("A{}", &sig[1..]) };
    let e = verify_err(&h.mgr, &format!("{}.{}.{}", h64, p64, bad_sig), None);
    assert_eq!(e.code, "AUT-207", "corrupted signature -> BadSignature");

    // Tamper the header (kid still known): signing input changes -> mismatch.
    let header = authsvc::json::parse(std::str::from_utf8(&authsvc::crypto::base64url_decode(h64).unwrap()).unwrap()).unwrap();
    let mut forged_h = header;
    forged_h.set("typ", Value::from("XWT"));
    let e = verify_err(
        &h.mgr,
        &format!("{}.{}.{}", authsvc::crypto::base64url_encode(forged_h.to_json().as_bytes()), p64, sig),
        None,
    );
    assert_eq!(e.code, "AUT-207", "tampered header -> BadSignature");
    assert_eq!(h.mgr.stats().verified_fail, 3, "each failed verify counted");
}

#[test]
fn wrong_key_unknown_kid_and_bad_algorithm() {
    let outsider = SigningKey::new("kid-rogue", b"rogue-secret-012345678".to_vec(), T0);
    let c = claims_at(T0, 60 * SECOND, "sub-b", vec!["read".to_string()]);
    let forged = raw_token(&outsider, &c);
    let e = verify_err(&harness().mgr, &forged, None);
    assert_eq!(e.code, "AUT-206", "kid outside the ring -> UnknownKey");

    // Known kid, signature computed with the wrong secret.
    let h = harness();
    let tok = issue(&h, "sub-b", &["read".to_string()], None);
    let segs: Vec<&str> = tok.token.splitn(3, '.').collect();
    let bad = authsvc::crypto::hmac_sha256(b"some-other-secret", format!("{}.{}", segs[0], segs[1]).as_bytes());
    let e = verify_err(&h.mgr, &format!("{}.{}.{}", segs[0], segs[1], authsvc::crypto::base64url_encode(&bad)), None);
    assert_eq!(e.code, "AUT-207", "wrong secret -> BadSignature");

    // alg not HS256.
    let header = Value::object(vec![("alg", Value::from("none")), ("kid", Value::from(BETA_KID))]);
    let c2 = claims_at(T0, 60 * SECOND, "sub-b", vec![]);
    let e = verify_err(&h.mgr, &raw_token_with_alg(&header, &c2, BETA_SECRET), None);
    assert_eq!(e.code, "AUT-205", "alg != HS256 -> BadAlgorithm");
}

fn raw_token_with_alg(header: &Value, c: &Claims, secret: &[u8]) -> String {
    let h64 = authsvc::crypto::base64url_encode(header.to_json().as_bytes());
    let p64 = authsvc::crypto::base64url_encode(c.to_json().to_json().as_bytes());
    let sig = authsvc::crypto::hmac_sha256(secret, format!("{}.{}", h64, p64).as_bytes());
    format!("{}.{}.{}", h64, p64, authsvc::crypto::base64url_encode(&sig))
}

#[test]
fn expired_via_manual_clock() {
    let h = harness();
    let tok = issue(&h, "sub-c", &[], None);
    assert!(h.mgr.verify(&tok.token, None).is_ok(), "fresh token is valid");

    let exp = T0 + h.cfg.token.ttl_ns;
    let skew = h.cfg.token.clock_skew_ns;
    h.clock.set(exp - skew + 1);
    assert!(h.mgr.verify(&tok.token, None).is_ok(), "1ns inside the skew window still valid");
    h.clock.set(exp + skew);
    assert!(h.mgr.verify(&tok.token, None).is_ok(), "exactly exp+skew still valid (strict >)");
    h.clock.set(exp + skew + 1);
    let e = verify_err(&h.mgr, &tok.token, None);
    assert_eq!(e.code, "AUT-208", "past exp+skew -> Expired");
    assert_eq!(e.status(), 400);

    // Boundary in a fresh harness: exactly exp+skew is valid, +1ns is not.
    let h2 = harness();
    let t2 = issue(&h2, "sub-d", &[], None);
    h2.clock.set(T0 + h2.cfg.token.ttl_ns + h2.cfg.token.clock_skew_ns);
    assert!(h2.mgr.verify(&t2.token, None).is_ok(), "edge of skew accepted");
    h2.clock.advance(1);
    assert_eq!(verify_err(&h2.mgr, &t2.token, None).code, "AUT-208");
}

#[test]
fn nbf_in_future_beyond_skew() {
    let h = harness();
    // Token minted 10s in the future (raw token; `issue` always uses now).
    let c = claims_at(T0 + 10 * SECOND, 600 * SECOND, "sub-e", vec!["read".to_string()]);
    let tok = raw_token(&h.beta, &c);
    let e = verify_err(&h.mgr, &tok, None);
    assert_eq!(e.code, "AUT-209", "nbf > now+skew -> NotYetValid");

    // Within skew: valid.
    let c2 = claims_at(T0 + 4 * SECOND, 600 * SECOND, "sub-f", vec![]);
    assert!(h.mgr.verify(&raw_token(&h.beta, &c2), None).is_ok(), "nbf 4s ahead is inside the 5s skew");

    // Advance the clock past nbf: becomes valid (scope check included).
    h.clock.set(T0 + 10 * SECOND);
    let vc = h.mgr.verify(&tok, Some("read")).expect("scope check now");
    assert_eq!(vc.claims.sub, "sub-e");
}

#[test]
fn ttl_override_capped_at_max_ttl() {
    let mut cfg = AuthConfig::default();
    cfg.token.ttl_ns = 60 * SECOND;
    cfg.token.max_ttl_ns = 10 * SECOND;
    let h = harness_cfg(cfg);

    let t_small = issue(&h, "sub-g", &[], Some(5 * SECOND));
    assert_eq!(t_small.exp, T0 + 5 * SECOND, "small override honored");

    let t_big = issue(&h, "sub-h", &[], Some(999 * SECOND));
    assert_eq!(t_big.exp, T0 + 10 * SECOND, "override clamped to max_ttl_ns");
    assert_eq!(t_big.nbf, T0);

    // Non-positive override is rejected before touching the clock.
    let e = h.mgr.issue("sub-i", &[], Some(0)).unwrap_err();
    assert_eq!(e.code, "AUT-203");
    assert_eq!(h.mgr.stats().issued, 2, "failed issues not counted");
}

#[test]
fn scopes_round_trip_and_required_scope() {
    let h = harness();
    let scopes = vec!["orders:read".to_string(), "orders:cancel".to_string(), "risk:hard".to_string()];
    let tok = issue(&h, "svc:strategy", &scopes, None);
    assert_eq!(tok.scope, scopes);

    let vc = h.mgr.verify(&tok.token, None).expect("no scope requested");
    assert_eq!(vc.claims.scope, scopes);

    // Required scope present -> pass.
    let vc = h.mgr.verify(&tok.token, Some("risk:hard")).expect("scope present");
    assert_eq!(vc.kid, BETA_KID);

    // Required scope absent -> AUT-211.
    let e = verify_err(&h.mgr, &tok.token, Some("portfolio:write"));
    assert_eq!(e.code, "AUT-211");
    assert!(e.message.contains("portfolio:write"));

    // Empty scope list verifies with no required scope but fails any request.
    let t_empty = issue(&h, "svc:empty", &[], None);
    assert!(h.mgr.verify(&t_empty.token, None).is_ok());
    assert_eq!(verify_err(&h.mgr, &t_empty.token, Some("read")).code, "AUT-211");
}

#[test]
fn malformed_and_missing_token() {
    let h = harness();
    // Two and four segments are both structurally invalid.
    assert_eq!(verify_err(&h.mgr, "abc.def", None).code, "AUT-204");
    assert_eq!(verify_err(&h.mgr, "a.b.c.d", None).code, "AUT-204");
    // Three segments but neither is JSON.
    let seg = authsvc::crypto::base64url_encode(b"not-json");
    assert_eq!(verify_err(&h.mgr, &format!("{}.{}.{}", seg, seg, seg), None).code, "AUT-204");
    // Empty / blank strings.
    assert_eq!(verify_err(&h.mgr, "", None).code, "AUT-204");
    assert_eq!(verify_err(&h.mgr, "   ", None).code, "AUT-204");

    // Valid signature but the payload lacks required claims (no jti).
    let header = Value::object(vec![("alg", Value::from("HS256")), ("kid", Value::from(BETA_KID))]);
    let payload = Value::object(vec![
        ("iss", Value::from("auth-service")),
        ("sub", Value::from("x")),
        ("iat", Value::from(T0)),
        ("nbf", Value::from(T0)),
        ("exp", Value::from(T0 + SECOND)),
    ]);
    let h64 = authsvc::crypto::base64url_encode(header.to_json().as_bytes());
    let p64 = authsvc::crypto::base64url_encode(payload.to_json().as_bytes());
    let sig = authsvc::crypto::hmac_sha256(BETA_SECRET, format!("{}.{}", h64, p64).as_bytes());
    let e = verify_err(&h.mgr, &format!("{}.{}.{}", h64, p64, authsvc::crypto::base64url_encode(&sig)), None);
    assert_eq!(e.code, "AUT-212", "claims parse failure -> SemanticMismatch");

    // Header without a kid: lookup of "" fails -> UnknownKey.
    let no_kid = Value::object(vec![("alg", Value::from("HS256"))]);
    let c3 = claims_at(T0, SECOND, "x", vec![]);
    let e = verify_err(&h.mgr, &raw_token_with_alg(&no_kid, &c3, BETA_SECRET), None);
    assert_eq!(e.code, "AUT-206", "missing kid in header -> UnknownKey(\"\")");

    // iat after exp is a semantic failure even if signature is valid.
    let bad = Claims {
        iss: "auth-service".to_string(),
        sub: "x".to_string(),
        aud: None,
        jti: "jti-bad".to_string(),
        iat: T0 + SECOND,
        nbf: T0 + SECOND,
        exp: T0,
        scope: vec![],
    };
    let e = verify_err(&h.mgr, &raw_token(&h.beta, &bad), None);
    assert_eq!(e.code, "AUT-212");

    // Every failure increments verified_fail.
    assert_eq!(h.mgr.stats().verified_fail, 8);
    // `AuthError` implements Display with the code.
    let e = AuthError::malformed_token("x");
    assert!(format!("{}", e).starts_with("AUT-204"));
}
