//! `api-gateway` (S15) — the external-facing REST gateway.
//!
//! Every external request enters here. The gateway:
//!   1. authenticates the bearer JWT by calling S12 `auth-service` `/verify`
//!   2. authorises the route against the caller's scopes
//!   3. reverse-proxies the (method, path) to the upstream service named by a
//!      built-in route table, forwarding the original query string and body
//!
//! It serves a small set of local endpoints directly: `healthz`, `readyz`,
//! `routes` (the route table) and `stats`.
//!
//! This crate is `std`-only: the JSON codec, router, TCP server and crypto
//! primitives are the shared, service-agnostic templates under `src/`
//! (`json.rs`, `router.rs`, `server.rs`, `crypto.rs`). Only the `config`,
//! `models`, `errors`, `core` and `http` modules are service-specific.

// Service-agnostic boilerplate — copied verbatim from `templates/rust/`.
pub mod json;
pub mod router;
pub mod server;
pub mod crypto;

// Service-specific modules.
pub mod config;
pub mod models;
pub mod errors;
pub mod core;
pub mod http;
