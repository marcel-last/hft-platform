//! S12 auth-service domain types: JWT claims, signing keys, verify results.
//!
//! Wire formats: compact JWT `b64url(header).b64url(payload).b64url(sig)` (HS256)
//! and a JSON claims object (iss/sub/aud/jti/iat/nbf/exp/scope).
//! All crypto primitives come from the copied `crypto.rs`.

use crate::crypto;
use crate::json::Value;

/// Default `iss` claim value.
pub const DEFAULT_ISSUER: &str = "auth-service";

/// Failure reasons for token verification, with stable wire strings.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VerifyFailure {
    /// Token did not parse into three base64url segments of JSON.
    Malformed,
    /// Header `alg` is not HS256.
    BadAlgorithm,
    /// Header `kid` is not in the key ring.
    UnknownKey,
    /// HMAC over `header.payload` does not match the signature.
    BadSignature,
    /// `exp` (plus skew) is in the past.
    Expired,
    /// `nbf` (minus skew) is in the future.
    NotYetValid,
    /// `jti` is in the revocation set.
    Revoked,
    /// A required scope is absent from the token.
    MissingScope,
    /// Claim values failed semantic checks (issuer, subject, ordering).
    SemanticMismatch,
}

impl VerifyFailure {
    /// Stable wire string used in the error envelope context.
    pub fn as_str(&self) -> &'static str {
        match self {
            VerifyFailure::Malformed => "malformed",
            VerifyFailure::BadAlgorithm => "bad_algorithm",
            VerifyFailure::UnknownKey => "unknown_key",
            VerifyFailure::BadSignature => "bad_signature",
            VerifyFailure::Expired => "expired",
            VerifyFailure::NotYetValid => "not_yet_valid",
            VerifyFailure::Revoked => "revoked",
            VerifyFailure::MissingScope => "missing_scope",
            VerifyFailure::SemanticMismatch => "semantic_mismatch",
        }
    }
}

/// JWT claim set.
#[derive(Debug, Clone, PartialEq)]
pub struct Claims {
    pub iss: String,
    pub sub: String,
    pub aud: Option<String>,
    pub jti: String,
    pub iat: i64,
    pub nbf: i64,
    pub exp: i64,
    pub scope: Vec<String>,
}

fn str_field(obj: &[(String, Value)], key: &str) -> Result<String, String> {
    obj.iter()
        .find(|(k, _)| k == key)
        .and_then(|(_, v)| v.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| format!("claim '{}' is missing or not a string", key))
}

fn int_field(obj: &[(String, Value)], key: &str) -> Result<i64, String> {
    obj.iter()
        .find(|(k, _)| k == key)
        .and_then(|(_, v)| v.as_i64())
        .ok_or_else(|| format!("claim '{}' is missing or not an integer", key))
}

impl Claims {
    /// Serialize to the JSON object that forms the JWT payload.
    pub fn to_json(&self) -> Value {
        let mut pairs: Vec<(&str, Value)> = Vec::with_capacity(8);
        pairs.push(("iss", Value::from(self.iss.as_str())));
        pairs.push(("sub", Value::from(self.sub.as_str())));
        if let Some(aud) = &self.aud {
            pairs.push(("aud", Value::from(aud.as_str())));
        }
        pairs.push(("jti", Value::from(self.jti.as_str())));
        pairs.push(("iat", Value::from(self.iat)));
        pairs.push(("nbf", Value::from(self.nbf)));
        pairs.push(("exp", Value::from(self.exp)));
        let scopes: Vec<Value> = self.scope.iter().map(|s| Value::from(s.as_str())).collect();
        pairs.push(("scope", Value::from(scopes)));
        Value::object(pairs)
    }

    /// Deserialize from a JSON object; single error message on failure.
    pub fn from_json(v: &Value) -> Result<Claims, String> {
        let obj = v.as_object().ok_or_else(|| "claims is not an object".to_string())?;
        let iss = str_field(obj, "iss")?;
        let sub = str_field(obj, "sub")?;
        let aud = match obj.iter().find(|(k, _)| k == "aud") {
            Some((_, val)) if val.is_null() => None,
            Some((_, _)) => Some(str_field(obj, "aud")?),
            None => None,
        };
        let jti = str_field(obj, "jti")?;
        let iat = int_field(obj, "iat")?;
        let nbf = int_field(obj, "nbf")?;
        let exp = int_field(obj, "exp")?;
        let scope = match obj.iter().find(|(k, _)| k == "scope") {
            Some((_, val)) => {
                let arr = val
                    .as_array()
                    .ok_or_else(|| "claim 'scope' is not an array".to_string())?;
                arr.iter()
                    .map(|s| {
                        s.as_str()
                            .map(|x| x.to_string())
                            .ok_or_else(|| "a scope entry is not a string".to_string())
                    })
                    .collect::<Result<Vec<String>, String>>()?
            }
            None => Vec::new(),
        };
        Ok(Claims { iss, sub, aud, jti, iat, nbf, exp, scope })
    }

    /// Time-window check with clock skew.
    pub fn check_time(&self, now_ns: i64, skew_ns: i64) -> Result<(), VerifyFailure> {
        if self.nbf > self.exp {
            return Err(VerifyFailure::SemanticMismatch);
        }
        if now_ns + skew_ns < self.nbf {
            return Err(VerifyFailure::NotYetValid);
        }
        if now_ns > self.exp + skew_ns {
            return Err(VerifyFailure::Expired);
        }
        Ok(())
    }
}

/// A signing key in the ring. The secret never leaves this struct;
/// `public_view()` is the only form ever exposed over HTTP.
#[derive(Debug, Clone)]
pub struct SigningKey {
    pub kid: String,
    pub secret: Vec<u8>,
    pub active: bool,
    pub created_ns: i64,
}

impl SigningKey {
    pub fn new(kid: &str, secret: Vec<u8>, created_ns: i64) -> Self {
        Self { kid: kid.to_string(), secret, active: true, created_ns }
    }

    /// Fresh randomized key: 32 random bytes, kid = "kid-" + base64url(8 random bytes).
    pub fn generate(created_ns: i64) -> std::io::Result<Self> {
        let secret = crypto::random_bytes(32)?;
        let suffix = crypto::random_token(8)?;
        Ok(Self::new(&format!("kid-{}", suffix), secret, created_ns))
    }

    /// First 16 hex chars of SHA-256 over the secret (stable key identity).
    pub fn fingerprint(&self) -> String {
        crypto::sha256_hex(&self.secret).chars().take(16).collect()
    }

    /// JWKS-style public view; never includes the secret.
    pub fn public_view(&self) -> Value {
        Value::object(vec![
            ("kid", Value::from(self.kid.as_str())),
            ("alg", Value::from("HS256")),
            ("fingerprint", Value::from(self.fingerprint().as_str())),
            ("created_ns", Value::from(self.created_ns)),
            ("active", Value::from(self.active)),
        ])
    }

    /// HS256 signature of `payload` (the `header.payload` string).
    pub fn sign(&self, payload: &str) -> Vec<u8> {
        crypto::hmac_sha256(&self.secret, payload.as_bytes()).to_vec()
    }

    /// Constant-time check of a decoded signature against `payload`.
    pub fn verify_signature(&self, payload: &str, sig: &[u8]) -> bool {
        crypto::constant_time_eq(&self.sign(payload), sig)
    }
}

/// Result of a successful token issuance.
#[derive(Debug, Clone, PartialEq)]
pub struct IssuedToken {
    pub token: String,
    pub kid: String,
    pub jti: String,
    pub sub: String,
    pub scope: Vec<String>,
    pub iat: i64,
    pub nbf: i64,
    pub exp: i64,
}

impl IssuedToken {
    pub fn to_json(&self) -> Value {
        let scopes: Vec<Value> = self.scope.iter().map(|s| Value::from(s.as_str())).collect();
        Value::object(vec![
            ("token", Value::from(self.token.as_str())),
            ("kid", Value::from(self.kid.as_str())),
            ("jti", Value::from(self.jti.as_str())),
            ("sub", Value::from(self.sub.as_str())),
            ("scope", Value::from(scopes)),
            ("iat", Value::from(self.iat)),
            ("nbf", Value::from(self.nbf)),
            ("exp", Value::from(self.exp)),
        ])
    }
}

/// Revocation result; `created` is true only for a first-time revocation.
#[derive(Debug, Clone, PartialEq)]
pub struct RevokeResult {
    pub jti: String,
    pub created: bool,
}

/// A token that passed every check (signature, time-window, revocation, scope).
#[derive(Debug, Clone)]
pub struct VerifiedClaims {
    pub claims: Claims,
    pub kid: String,
}

impl VerifiedClaims {
    pub fn to_json(&self) -> Value {
        let mut v = self.claims.to_json();
        v.set("kid", Value::from(self.kid.as_str()));
        v
    }
}

/// Assemble `b64url(header).b64url(payload).b64url(signature)`.
pub fn jwt_encode(header: &Value, payload: &Value, signature: &[u8]) -> String {
    format!(
        "{}.{}.{}",
        crypto::base64url_encode(header.to_json().as_bytes()),
        crypto::base64url_encode(payload.to_json().as_bytes()),
        crypto::base64url_encode(signature)
    )
}

/// Parse a compact JWT into (header, payload, decoded signature).
pub fn jwt_decode(token: &str) -> Result<(Value, Value, Vec<u8>), String> {
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 {
        return Err(format!("expected 3 base64url segments, found {}", parts.len()));
    }
    let header_raw = crypto::base64url_decode(parts[0]).map_err(|e| e.message)?;
    let payload_raw = crypto::base64url_decode(parts[1]).map_err(|e| e.message)?;
    let sig = crypto::base64url_decode(parts[2]).map_err(|e| e.message)?;
    let header_str = String::from_utf8_lossy(&header_raw).into_owned();
    let header = crate::json::parse(&header_str).map_err(|e| e.message)?;
    let payload_str = String::from_utf8_lossy(&payload_raw).into_owned();
    let payload = crate::json::parse(&payload_str).map_err(|e| e.message)?;
    Ok((header, payload, sig))
}
