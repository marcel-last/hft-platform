//! S12 auth-service — JWT (HS256) issue / verify / revoke for the HFT platform.
//!
//! Library root. The four template modules (`json`, `router`, `server`,
//! `crypto`) are copied byte-identical from `templates/rust/` and are never
//! edited in this crate. Service-specific modules: `config`, `models`,
//! `errors`, `core` (AuthManager + Clock), `http` (handlers only).

pub mod config;
pub mod core;
pub mod crypto;
pub mod errors;
pub mod http;
pub mod json;
pub mod models;
pub mod router;
pub mod server;
