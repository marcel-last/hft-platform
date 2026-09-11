//! Immutable configuration for the `api-gateway` service.
//!
//! Follows the same spirit as the Python services' `config.py`: a frozen set of
//! structs plus a `validate()` that returns a list of human-readable problems
//! (empty list = valid). Environment/CLI overrides are applied in `main.rs`,
//! not here — this module reads no environment variables.

use std::fmt;

/// Service directory name (used in the §1.2 error envelope and `/healthz`).
pub const SERVICE_NAME: &str = "api-gateway";
/// Reported version (see CONVENTIONS §12).
pub const SERVICE_VERSION: &str = "1.0.0";
/// Default listening port for S15.
pub const DEFAULT_PORT: u16 = 7750;
/// Default bind address.
pub const DEFAULT_BIND: &str = "0.0.0.0";

/// A single upstream service the gateway can reverse-proxy to.
#[derive(Debug, Clone)]
pub struct Upstream {
    /// Resolvable host (e.g. `"127.0.0.1"`).
    pub host: String,
    /// Listening TCP port.
    pub port: u16,
}

impl Upstream {
    /// Build an upstream reference.
    pub fn new(host: impl Into<String>, port: u16) -> Self {
        Upstream { host: host.into(), port }
    }

    /// `(host, port)` pair suitable for `ToSocketAddrs`.
    pub fn addr(&self) -> (String, u16) {
        (self.host.clone(), self.port)
    }

    /// Absolute base URL, for diagnostics/logging only (the gateway always
    /// dials the TCP socket directly).
    pub fn base_url(&self) -> String {
        format!("http://{}:{}", self.host, self.port)
    }
}

impl fmt::Display for Upstream {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.host, self.port)
    }
}

/// Top-level gateway configuration.
#[derive(Debug, Clone)]
pub struct GatewayConfig {
    /// Service name (directory name); echoed in the error envelope.
    pub name: String,
    /// Service version.
    pub version: String,
    /// Bind address for the listener.
    pub bind: String,
    /// Listening port.
    pub port: u16,
    /// S12 `auth-service` — where bearer tokens are validated.
    pub auth: Upstream,
    /// S9 `portfolio-analytics` — the `/portfolio/**` upstream.
    pub portfolio: Upstream,
    /// S14 `settlement-service` — the `/settlement/**` upstream.
    pub settlement: Upstream,
    /// Outbound timeout (ms) for the token-validation call to S12.
    pub verify_timeout_ms: u64,
    /// Outbound timeout (ms) for each proxied request to an upstream.
    pub proxy_timeout_ms: u64,
    /// Maximum request body the gateway will accept before it answers 413/protocol.
    pub max_body_bytes: usize,
}

impl Default for GatewayConfig {
    fn default() -> Self {
        GatewayConfig {
            name: SERVICE_NAME.to_string(),
            version: SERVICE_VERSION.to_string(),
            bind: DEFAULT_BIND.to_string(),
            port: DEFAULT_PORT,
            auth: Upstream::new("127.0.0.1", 7720),
            portfolio: Upstream::new("127.0.0.1", 7690),
            settlement: Upstream::new("127.0.0.1", 7740),
            verify_timeout_ms: 800,
            proxy_timeout_ms: 1500,
            max_body_bytes: 1024 * 1024, // 1 MiB, matching the server template default
        }
    }
}

impl GatewayConfig {
    /// Return a list of human-readable problems; an empty list means the
    /// configuration is valid. Mirrors `validate_config()` in the Python
    /// services.
    pub fn validate(&self) -> Vec<String> {
        let mut errs: Vec<String> = Vec::new();

        if self.name.trim().is_empty() {
            errs.push("config.name must be non-empty".to_string());
        }
        if self.bind.trim().is_empty() {
            errs.push("config.bind must be non-empty".to_string());
        }
        if self.port == 0 {
            errs.push("config.port must be > 0".to_string());
        }
        if self.verify_timeout_ms == 0 {
            errs.push("config.verify_timeout_ms must be > 0".to_string());
        }
        if self.proxy_timeout_ms == 0 {
            errs.push("config.proxy_timeout_ms must be > 0".to_string());
        }
        if self.max_body_bytes == 0 {
            errs.push("config.max_body_bytes must be > 0".to_string());
        }

        for (label, up) in [
            ("auth", &self.auth),
            ("portfolio", &self.portfolio),
            ("settlement", &self.settlement),
        ] {
            if up.host.trim().is_empty() {
                errs.push(format!("config.{}.host must be non-empty", label));
            }
            if up.port == 0 {
                errs.push(format!("config.{}.port must be > 0", label));
            }
        }

        errs
    }

    /// Convenience for `main`: `Ok(())` when valid, else the list of problems.
    pub fn require_valid(&self) -> Result<(), Vec<String>> {
        let errs = self.validate();
        if errs.is_empty() {
            Ok(())
        } else {
            Err(errs)
        }
    }
}
