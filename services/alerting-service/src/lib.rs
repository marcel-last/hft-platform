//! alerting_service (S10) — library crate.
//!
//! Public API for tests and for the binary entrypoint in `main.rs`.  The crate
//! is std-only: no external dependencies.

pub mod config;
pub mod core;
pub mod errors;
pub mod http;
pub mod json;
pub mod models;
pub mod router;

/// Build the default service configuration (used by tests and main).
pub fn default_config() -> config::Config {
    config::Config::default()
}
