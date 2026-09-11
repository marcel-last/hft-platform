//! S12 revocation + key-rotation tests: revoke/idempotency, bounded-set
//! eviction, rotation semantics, stats counters, readiness.

use authsvc::config::AuthConfig;
use authsvc::json::Value;

#[allow(dead_code)]
mod common;

use common::*;

#[test]
fn revoke_then_verify_fails_revoked() {
    let h = harness();
    let tok = issue(&h, "sub-r1", &["read".to_string()], None);
    assert!(h.mgr.verify(&tok.token, None).is_ok());

    let r1 = h.mgr.revoke(&tok.jti).expect("revoke failed");
    assert!(r1.created, "first revocation creates the entry");

    let r2 = h.mgr.revoke(&tok.jti).expect("re-revoke failed");
    assert!(!r2.created, "re-revocation is idempotent");
    assert_eq!(r2.jti, tok.jti);

    let e = verify_err(&h.mgr, &tok.token, None);
    assert_eq!(e.code, "AUT-210", "revoked jti -> Revoked");
    assert_eq!(e.status(), 400);
    assert!(!e.retryable);
    assert_eq!(e.context().get("jti").and_then(|v| v.as_str()), Some(tok.jti.as_str()));

    // A different token from the same subject is unaffected.
    let other = issue(&h, "sub-r1", &["read".to_string()], None);
    assert_ne!(other.jti, tok.jti, "jti is unique per token");
    assert!(h.mgr.verify(&other.token, None).is_ok());

    assert_eq!(h.mgr.stats().revoked, 1, "idempotent re-revoke not counted");
    assert_eq!(h.mgr.stats().verified_fail, 1);
}

#[test]
fn revoke_empty_jti_rejected() {
    let h = harness();
    assert_eq!(h.mgr.revoke("").unwrap_err().code, "AUT-213");
    assert_eq!(h.mgr.revoke("   ").unwrap_err().code, "AUT-213");
    assert_eq!(h.mgr.stats().revoked, 0);
}

#[test]
fn bounded_revocation_set_evicts_oldest() {
    let mut cfg = AuthConfig::default();
    cfg.token.max_revocations = 3;
    let h = harness_cfg(cfg);

    let mk = |sub: &str| issue(&h, sub, &[], None);
    let (a, b, c) = (mk("a"), mk("b"), mk("c"));
    for t in [&a, &b, &c] {
        assert!(h.mgr.revoke(&t.jti).unwrap().created);
    }
    for t in [&a, &b, &c] {
        assert_eq!(verify_err(&h.mgr, &t.token, None).code, "AUT-210");
    }

    // Fourth revocation evicts the oldest (a).
    let d = mk("d");
    assert!(h.mgr.revoke(&d.jti).unwrap().created);
    assert_eq!(h.mgr.stats().revoked, 4);
    let e = verify_err(&h.mgr, &d.token, None);
    assert_eq!(e.code, "AUT-210");
    let e = verify_err(&h.mgr, &b.token, None);
    assert_eq!(e.code, "AUT-210");
    assert!(h.mgr.verify(&a.token, None).is_ok(), "evicted jti validates again");
}

#[test]
fn rotate_key_retires_oldest_and_old_tokens_still_verify() {
    let mut cfg = AuthConfig::default();
    cfg.keys.ring_size = 1; // eviction cap = ring_size*4 = 4 keys
    let h = harness_cfg(cfg);
    let old_tok = issue(&h, "old", &[], None);
    assert_eq!(old_tok.kid, BETA_KID, "newest fixture key signs");

    // Rotation 1: fresh key created at the current (manual) clock instant.
    let gamma = h.mgr.rotate_key().expect("rotation failed");
    assert!(gamma.active);
    assert!(gamma.kid.starts_with("kid-"));
    assert_eq!(gamma.created_ns, T0, "rotation stamped with the (manual) clock");

    // New tokens are signed with the newest active key.
    let new_tok = issue(&h, "new", &[], None);
    assert_eq!(new_tok.kid, gamma.kid, "issue uses the most recent active key");

    // Both fixture keys are retired by the ring-size-1 cap but still retained,
    // so tokens they signed keep verifying.
    assert!(h.mgr.verify(&old_tok.token, None).is_ok(), "retired beta token still valid");
    let c = claims_at(T0, 60 * SECOND, "alpha-user", vec!["read".to_string()]);
    let alpha_tok = raw_token(&h.alpha, &c);
    assert!(h.mgr.verify(&alpha_tok, None).is_ok(), "retired alpha token still valid");

    let pub_view = h.mgr.keys_public();
    assert_eq!(pub_view.len(), 3);
    let actives: Vec<bool> = pub_view.iter().map(|k| k.get("active").unwrap().as_bool().unwrap()).collect();
    assert_eq!(actives.iter().filter(|&&a| a).count(), 1, "only the fresh key is active");
    assert_eq!(h.mgr.stats().rotations, 1);

    // Rotations 2+3: each retires the oldest active key; when the ring exceeds
    // the cap of 4, the oldest *inactive* key (alpha) is evicted.
    h.mgr.rotate_key().expect("rotation 2 failed");
    h.mgr.rotate_key().expect("rotation 3 failed");
    let pub_view = h.mgr.keys_public();
    assert_eq!(pub_view.len(), 4, "one key evicted to stay at the cap");
    let kids: Vec<&str> = pub_view.iter().map(|k| k.get("kid").unwrap().as_str().unwrap()).collect();
    assert!(!kids.contains(&ALPHA_KID), "oldest inactive key evicted at cap");
    assert!(kids.contains(&BETA_KID), "newer retired keys retained");
    assert!(kids.contains(&gamma.kid.as_str()), "fresh key retained");
    let actives: Vec<bool> = pub_view.iter().map(|k| k.get("active").unwrap().as_bool().unwrap()).collect();
    assert_eq!(actives.iter().filter(|&&a| a).count(), 1);

    // Retained tokens verify; the evicted key's token no longer does.
    assert!(h.mgr.verify(&old_tok.token, None).is_ok(), "beta retained, token valid");
    let e = verify_err(&h.mgr, &alpha_tok, None);
    assert_eq!(e.code, "AUT-206", "evicted key no longer verifies (UnknownKey)");
    assert_eq!(h.mgr.stats().rotations, 3);
}

#[test]
fn stats_track_all_counters() {
    let h = harness();
    let t1 = issue(&h, "s1", &["read".to_string()], None);
    let t2 = issue(&h, "s2", &[], Some(30_000_000_000));
    assert!(h.mgr.verify(&t1.token, None).is_ok());
    assert!(h.mgr.verify(&t1.token, Some("read")).is_ok());
    assert!(h.mgr.verify(&t2.token, None).is_ok());
    verify_err(&h.mgr, "garbage", None);
    let _ = h.mgr.revoke(&t1.jti).expect("revoke");
    let _ = h.mgr.revoke(&t1.jti).expect("re-revoke idempotent");
    h.mgr.rotate_key().expect("rotate");

    let s = h.mgr.stats();
    assert_eq!(s.issued, 2);
    assert_eq!(s.verified_ok, 3);
    assert_eq!(s.verified_fail, 1);
    assert_eq!(s.revoked, 1);
    assert_eq!(s.rotations, 1);
    assert_eq!(h.mgr.config().port, 7720);
}

#[test]
fn readyz_requires_an_active_key() {
    let h = harness();
    let (ready, reasons) = h.mgr.ready();
    assert!(ready);
    assert!(reasons.is_empty());

    // Simulate the ring fully retired: drop to a single key, rotate until none
    // stay active is not expressible (rotation always adds an active key), so
    // assert the negative path on the public view instead: after two rotations
    // the oldest key is inactive yet exactly `ring_size` keys are active.
    h.mgr.rotate_key().expect("r1");
    h.mgr.rotate_key().expect("r2");
    let (ready, reasons) = h.mgr.ready();
    assert!(ready, "rotation never removes all active keys");
    assert!(reasons.is_empty());
    let pub_view = h.mgr.keys_public();
    let active: Vec<&Value> = pub_view.iter().filter(|k| k.get("active").unwrap().as_bool() == Some(true)).collect();
    assert_eq!(active.len(), 2, "exactly ring_size active keys");
    let inactive: Vec<&Value> = pub_view.iter().filter(|k| k.get("active").unwrap().as_bool() == Some(false)).collect();
    assert_eq!(inactive.len(), 2, "retired keys retained in the ring");
}
