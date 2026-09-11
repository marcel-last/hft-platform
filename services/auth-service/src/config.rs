//! S12 auth-service configuration.
//!
//! Immutable config checked once at boot. No environment reading lives here
//! (that is the config-service's job at deploy time); `main.rs` builds the
//! config from flags/environment and calls `require_valid()` before serving.

/// Service directory name — used as the `service` field of every error envelope.
pub const SERVICE_NAME: &str = "auth-service";

/// Service version, surfaced in `/healthz` and the Cargo metadata.
pub const SERVICE_VERSION: &str = "1.0.0";

/// Default TCP port for the auth service.
pub const DEFAULT_PORT: u16 = 7720;

/// Default token time-to-live: ~5 minutes in nanoseconds.
pub const DEFAULT_TTL_NS: i64 = 61_000_000_000;

/// Default clock skew allowance applied to `nbf` / `exp` checks, in ns (~5 s).
pub const DEFAULT_CLOCK_SKEW_NS: i64 = 5_000_000_000;

/// Default hard cap on any per-token TTL override, in ns (~1 hour).
pub const DEFAULT_MAX_TTL_NS: i64 = 3_600_000_000_000;

/// Default cap on the in-memory revocation set (oldest entries evicted first).
pub const DEFAULT_MAX_REVOCATIONS: usize = 10_000;

/// Token lifecycle bounds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenConfig {
    /// Time-to-live applied when the issuer does not pass `ttl_ns`.
    pub ttl_ns: i64,
    /// Clock skew allowed when checking `nbf` and `exp`.
    pub clock_skew_ns: i64,
    /// Hard cap on any per-token `ttl_ns` override.
    pub max_ttl_ns: i64,
    /// Maximum number of entries kept in the revocation set.
    pub max_revocations: usize,
}

impl Default for TokenConfig {
    fn default() -> Self {
        Self {
            ttl_ns: DEFAULT_TTL_NS,
            clock_skew_ns: DEFAULT_CLOCK_SKEW_NS,
            max_ttl_ns: DEFAULT_MAX_TTL_NS,
            max_revocations: DEFAULT_MAX_REVOCATIONS,
        }
    }
}

/// Signing-key ring sizing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KeyConfig {
    /// Number of active keys kept in the ring after each rotation.
    pub ring_size: usize,
}

impl Default for KeyConfig {
    fn default() -> Self {
        Self { ring_size: 2 }
    }
}

/// Full auth-service configuration.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AuthConfig {
    pub name: &'static str,
    pub version: &'static str,
    pub port: u16,
    pub token: TokenConfig,
    pub keys: KeyConfig,
}

impl Default for AuthConfig {
    fn default() -> Self {
        Self {
            name: SERVICE_NAME,
            version: SERVICE_VERSION,
            port: DEFAULT_PORT,
            token: TokenConfig::default(),
            keys: KeyConfig::default(),
        }
    }
}

impl AuthConfig {
    /// Returns a list of human-readable problems; an empty list means valid.
    pub fn validate(&self) -> Vec<String> {
        let mut errs: Vec<String> = Vec::new();
        if self.name.is_empty() {
            errs.push("name must not be empty".to_string());
        }
        if self.version.is_empty() {
            errs.push("version must not be empty".to_string());
        }
        if self.port == 0 {
            errs.push("port must be non-zero".to_string());
        }
        if self.token.ttl_ns <= 0 {
            errs.push("token.ttl_ns must be > 0".to_string());
        }
        if self.token.clock_skew_ns < 0 {
            errs.push("token.clock_skew_ns must be >= 0".to_string());
        }
        if self.token.max_ttl_ns < self.token.ttl_ns {
            errs.push("token.max_ttl_ns must be >= token.ttl_ns".to_string());
        }
        if self.token.max_revocations < 1 {
            errs.push("token.max_revocations must be >= 1".to_string());
        }
        if self.keys.ring_size < 1 {
            errs.push("keys.ring_size must be >= 1".to_string());
        }
        errs
    }

    /// Boot-time gate: logs every problem and exits the process if invalid.
    pub fn require_valid(&self) {
        let errs = self.validate();
        if !errs.is_empty() {
            for e in &errs {
                eprintln!("auth-service config error: {}", e);
            }
            std::process::exit(1);
        }
    }
}
