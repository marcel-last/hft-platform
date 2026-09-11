//! S12 auth-service core: `AuthManager` (issue / verify / revoke / rotate) and
//! the `Clock` abstraction that makes time-based behaviour deterministic in tests.
//!
//! Production wires in a `SystemClock`; tests inject a `ManualClock` (atomic,
//! `Arc`-shareable) so expiry / not-before / revocation windows need no sleeping.

use crate::config::AuthConfig;
use crate::crypto;
use crate::errors::AuthError;
use crate::json::Value;
use crate::models::{Claims, IssuedToken, RevokeResult, SigningKey, VerifiedClaims, VerifyFailure};
use std::collections::{HashSet, VecDeque};
use std::sync::atomic::AtomicI64;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

/// Wall-clock abstraction. `now_ns` returns int64 nanoseconds since the Unix epoch.
pub trait Clock: Send + Sync {
    /// Current time in int64 nanoseconds since the Unix epoch.
    fn now_ns(&self) -> i64;
}

/// Production clock backed by the system time.
#[derive(Debug, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ns(&self) -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as i64)
            .unwrap_or(0)
    }
}

/// Deterministic clock for tests: set an absolute instant or advance by a delta.
#[derive(Debug)]
pub struct ManualClock(AtomicI64);

impl ManualClock {
    /// Create a clock fixed at `t0` (int64 ns).
    pub fn new(t0: i64) -> Self {
        Self(AtomicI64::new(t0))
    }
    /// Set the current instant to an absolute value.
    pub fn set(&self, t: i64) {
        self.0.store(t, std::sync::atomic::Ordering::SeqCst);
    }
    /// Advance the clock by a delta (ns), positive or negative.
    pub fn advance(&self, delta_ns: i64) {
        self.0.fetch_add(delta_ns, std::sync::atomic::Ordering::SeqCst);
    }
    /// Read the current instant.
    pub fn get(&self) -> i64 {
        self.0.load(std::sync::atomic::Ordering::SeqCst)
    }
}

impl Clock for ManualClock {
    fn now_ns(&self) -> i64 {
        self.get()
    }
}

/// `Arc<Clock>` is itself a `Clock`, so a shared clock can cross API boundaries cheaply.
impl<T: Clock + ?Sized> Clock for Arc<T> {
    fn now_ns(&self) -> i64 {
        (**self).now_ns()
    }
}

/// Monotonic counters surfaced by `GET /stats`.
#[derive(Debug, Default, Clone)]
pub struct Stats {
    /// Successful `POST /token` issuances.
    pub issued: u64,
    /// `POST /verify` calls that returned a valid token.
    pub verified_ok: u64,
    /// `POST /verify` calls that returned an invalid token.
    pub verified_fail: u64,
    /// Successful `POST /revoke` calls (first-time only).
    pub revoked: u64,
    /// `rotate_key` calls.
    pub rotations: u64,
}

/// Mutable, Mutex-guarded state: key ring + bounded revocation set + stats.
struct AuthState {
    keys: Vec<SigningKey>,
    revoked: HashSet<String>,
    order: VecDeque<String>,
    max_revocations: usize,
    stats: Stats,
}

impl AuthState {
    fn new(keys: Vec<SigningKey>, max_revocations: usize) -> Self {
        Self {
            keys,
            revoked: HashSet::new(),
            order: VecDeque::new(),
            max_revocations,
            stats: Stats::default(),
        }
    }

    /// True if `jti` is in the revocation set.
    fn is_revoked(&self, jti: &str) -> bool {
        self.revoked.contains(jti)
    }

    /// Insert `jti`; returns true only on a first-time insertion. Evicts the oldest
    /// entries so the set never exceeds `max_revocations`.
    fn revoke(&mut self, jti: &str) -> bool {
        if self.revoked.contains(jti) {
            return false;
        }
        self.revoked.insert(jti.to_string());
        self.order.push_back(jti.to_string());
        while self.order.len() > self.max_revocations {
            if let Some(old) = self.order.pop_front() {
                self.revoked.remove(&old);
            }
        }
        self.stats.revoked += 1;
        true
    }
}

/// Thread-safe token authority: signing-key ring, bounded revocation set, stats.
pub struct AuthManager {
    clock: Arc<dyn Clock>,
    cfg: AuthConfig,
    state: Mutex<AuthState>,
}

impl AuthManager {
    fn lock(&self) -> std::sync::MutexGuard<'_, AuthState> {
        self.state.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// Production constructor: system clock plus one or more pre-built keys.
    pub fn new(cfg: AuthConfig, initial_keys: Vec<SigningKey>) -> Self {
        Self::with_clock(cfg, initial_keys, Arc::new(SystemClock))
    }

    /// Constructor with an injected clock (tests use `Arc<ManualClock>`).
    pub fn with_clock(cfg: AuthConfig, initial_keys: Vec<SigningKey>, clock: Arc<dyn Clock>) -> Self {
        Self {
            clock,
            cfg,
            state: Mutex::new(AuthState::new(
                initial_keys,
                cfg.token.max_revocations,
            )),
        }
    }

    /// Mint a new HS256 token for `sub` with the given scopes and optional TTL override.
    pub fn issue(&self, sub: &str, scopes: &[String], ttl: Option<i64>) -> Result<IssuedToken, AuthError> {
        if sub.trim().is_empty() {
            return Err(AuthError::missing_subject("empty or missing 'sub'"));
        }
        let now = self.clock.now_ns();

        // Resolve the effective TTL: request override (clamped) or the configured default.
        let ttl_ns = match ttl {
            Some(t) if t > 0 => t.min(self.cfg.token.max_ttl_ns),
            Some(_) => return Err(AuthError::bad_ttl("ttl_ns must be > 0")),
            None => self.cfg.token.ttl_ns,
        };

        // Select the most recently created active key as the signer.
        let key = {
            let s = self.lock();
            s.keys
                .iter()
                .filter(|k| k.active)
                .max_by_key(|k| k.created_ns)
                .cloned()
                .ok_or_else(|| AuthError::semantic_mismatch("no active signing key"))?
        };

        let jti = match crypto::random_token(16) {
            Ok(t) => t,
            Err(e) => return Err(AuthError::key_generation_failed(&e.to_string())),
        };

        let claims = Claims {
            iss: crate::models::DEFAULT_ISSUER.to_string(),
            sub: sub.to_string(),
            aud: None,
            jti: jti.clone(),
            iat: now,
            nbf: now,
            exp: now + ttl_ns,
            scope: scopes.to_vec(),
        };

        let header = Value::object(vec![
            ("alg", Value::from("HS256")),
            ("typ", Value::from("JWT")),
            ("kid", Value::from(key.kid.as_str())),
        ]);
        let payload = claims.to_json();
        let header_b64 = crypto::base64url_encode(header.to_json().as_bytes());
        let payload_b64 = crypto::base64url_encode(payload.to_json().as_bytes());
        let signing_input = format!("{}.{}", header_b64, payload_b64);
        let sig = key.sign(&signing_input);
        let token = crate::models::jwt_encode(&header, &payload, &sig);

        {
            let mut s = self.lock();
            s.stats.issued += 1;
        }

        Ok(IssuedToken {
            token,
            kid: key.kid,
            jti,
            sub: sub.to_string(),
            scope: scopes.to_vec(),
            iat: now,
            nbf: now,
            exp: now + ttl_ns,
        })
    }

    /// Full verification pipeline: structure -> alg -> kid -> constant-time HMAC ->
    /// claims -> time window (skew) -> revocation -> optional required scope.
    pub fn verify(&self, token: &str, required_scope: Option<&str>) -> Result<VerifiedClaims, AuthError> {
        let res = self.verify_inner(token, required_scope);
        let mut s = self.lock();
        match &res {
            Ok(_) => s.stats.verified_ok += 1,
            Err(_) => s.stats.verified_fail += 1,
        }
        res
    }

    fn verify_inner(&self, token: &str, required_scope: Option<&str>) -> Result<VerifiedClaims, AuthError> {
        if token.trim().is_empty() {
            return Err(AuthError::malformed_token("empty token"));
        }
        let (header, payload, sig) =
            crate::models::jwt_decode(token).map_err(|d| AuthError::malformed_token(&d))?;
        let alg = header.get("alg").and_then(|v| v.as_str()).unwrap_or("");
        if alg != "HS256" {
            return Err(AuthError::bad_algorithm(alg));
        }
        let kid = header.get("kid").and_then(|v| v.as_str()).unwrap_or("");
        if kid.is_empty() {
            return Err(AuthError::unknown_key(""));
        }
        let claims = Claims::from_json(&payload)
            .map_err(|d| AuthError::semantic_mismatch(&d))?;
        if claims.iat > claims.exp {
            return Err(AuthError::semantic_mismatch("iat is after exp"));
        }

        // Look the key up by kid, then check the HMAC in constant time.
        // The signing input is the literal `header.payload` prefix of the token.
        let key = {
            let s = self.lock();
            s.keys
                .iter()
                .find(|k| k.kid == kid)
                .cloned()
                .ok_or_else(|| AuthError::unknown_key(kid))?
        };
        let dot = token.find('.').ok_or_else(|| AuthError::malformed_token("no header segment"))?;
        let second = token[dot + 1..]
            .find('.')
            .map(|i| dot + 1 + i)
            .ok_or_else(|| AuthError::malformed_token("no payload segment"))?;
        if !key.verify_signature(&token[..second], &sig) {
            return Err(AuthError::bad_signature(kid));
        }

        let now = self.clock.now_ns();
        let skew = self.cfg.token.clock_skew_ns;
        let time_fail = claims.check_time(now, skew);
        if let Err(f) = time_fail {
            return Err(match f {
                VerifyFailure::Expired => AuthError::expired(claims.exp, now),
                VerifyFailure::NotYetValid => AuthError::not_yet_valid(claims.nbf, now),
                _ => AuthError::semantic_mismatch(f.as_str()),
            });
        }
        let revoked = self.lock().is_revoked(&claims.jti);
        if revoked {
            return Err(AuthError::revoked(&claims.jti));
        }
        if let Some(req) = required_scope {
            if !claims.scope.iter().any(|s| s == req) {
                return Err(AuthError::missing_scope(req));
            }
        }
        Ok(VerifiedClaims { claims, kid: key.kid })
    }

    /// Revoke a `jti`. Idempotent: `created` is true only on a first-time insertion.
    pub fn revoke(&self, jti: &str) -> Result<RevokeResult, AuthError> {
        if jti.trim().is_empty() {
            return Err(AuthError::missing_jti());
        }
        let created = {
            let mut s = self.lock();
            s.revoke(jti)
        };
        Ok(RevokeResult { jti: jti.to_string(), created })
    }

    /// Rotate the key ring: generate a fresh active key and retire the oldest active
    /// key(s) so at most `keys.ring_size` keys stay active. Retired keys remain in the
    /// ring (so already-issued tokens keep verifying) until the ring cap evicts them.
    pub fn rotate_key(&self) -> Result<SigningKey, AuthError> {
        let now = self.clock.now_ns();
        let fresh = SigningKey::generate(now)
            .map_err(|e| AuthError::key_generation_failed(&e.to_string()))?;
        let mut s = self.lock();
        s.keys.push(fresh.clone());
        s.stats.rotations += 1;
        let active: Vec<usize> = (0..s.keys.len()).filter(|&i| s.keys[i].active).collect();
        if active.len() > self.cfg.keys.ring_size {
            for &i in &active[..active.len() - self.cfg.keys.ring_size] {
                s.keys[i].active = false;
            }
        }
        let cap = self.cfg.keys.ring_size.saturating_mul(4);
        while s.keys.len() > cap {
            match s.keys.iter().position(|k| !k.active) {
                Some(i) => { s.keys.remove(i); }
                None => break,
            }
        }
        Ok(fresh)
    }

    /// Snapshot of the counters.
    pub fn stats(&self) -> Stats {
        self.lock().stats.clone()
    }

    /// Public view of every key in the ring (never the secrets).
    pub fn keys_public(&self) -> Vec<Value> {
        self.lock().keys.iter().map(|k| k.public_view()).collect()
    }

    /// Readiness: at least one active signing key. Returns (ready, reasons).
    pub fn ready(&self) -> (bool, Vec<String>) {
        let s = self.lock();
        let active = s.keys.iter().any(|k| k.active);
        let mut reasons: Vec<String> = Vec::new();
        if !active {
            reasons.push("no active signing key".to_string());
        }
        (active, reasons)
    }

    /// The validated configuration.
    pub fn config(&self) -> &AuthConfig {
        &self.cfg
    }
}
