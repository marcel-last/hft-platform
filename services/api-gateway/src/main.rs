//! `api-gateway` entrypoint.
//!
//! Parses `--bind` / `--port` (both `--flag value` and `--flag=value` forms),
//! loads the gateway config (defaults, overridable via the environment),
//! validates it, constructs the production `Gateway` over `HttpTransport`,
//! starts the listener and blocks on the accept loop.
//!
//! Environment overrides (all optional; defaults in `config.rs` apply otherwise):
//!   * `APIGW_BIND` / `APIGW_PORT`
//!   * `APIGW_AUTH_HOST` / `APIGW_AUTH_PORT`          (S12 auth-service)
//!   * `APIGW_PORTFOLIO_HOST` / `APIGW_PORTFOLIO_PORT` (S9)
//!   * `APIGW_SETTLEMENT_HOST` / `APIGW_SETTLEMENT_PORT` (S14)
//!   * `APIGW_VERIFY_TIMEOUT_MS` / `APIGW_PROXY_TIMEOUT_MS`

use std::env;
use std::process;

use apigw::config::{GatewayConfig, SERVICE_NAME, SERVICE_VERSION};
use apigw::core::{Gateway, HttpTransport};
use apigw::http;
use std::sync::Arc;

/// A single CLI argument as parsed.
struct Arg {
    name: String,
    value: String,
}

/// Parse `--flag value` and `--flag=value` pairs. Returns the unknown-flag
/// name (if any) so the caller can exit 2.
fn parse_args() -> (Vec<Arg>, Option<String>) {
    let mut out: Vec<Arg> = Vec::new();
    let argv: Vec<String> = env::args().skip(1).collect();
    let mut i = 0usize;
    while i < argv.len() {
        let tok = &argv[i];
        if let Some(stripped) = tok.strip_prefix("--") {
            if let Some(eq) = stripped.find('=') {
                let name = stripped[..eq].to_string();
                let value = stripped[eq + 1..].to_string();
                out.push(Arg { name, value });
            } else {
                let name = stripped.to_string();
                i += 1;
                if i < argv.len() {
                    let value = argv[i].clone();
                    out.push(Arg { name, value });
                } else {
                    return (out, Some(name)); // flag without a value
                }
            }
        }
        i += 1;
    }
    (out, None)
}

fn env_str(key: &str) -> Option<String> {
    env::var(key).ok().filter(|v| !v.is_empty())
}

fn env_u16(key: &str) -> Option<u16> {
    env_str(key).and_then(|v| v.parse().ok())
}

fn env_u64(key: &str) -> Option<u64> {
    env_str(key).and_then(|v| v.parse().ok())
}

fn main() {
    let (args, unknown) = parse_args();
    if let Some(name) = unknown {
        eprintln!("api-gateway: unknown or missing-value flag: --{}", name);
        process::exit(2);
    }

    let mut cfg = GatewayConfig::default();

    // Environment overrides.
    if let Some(v) = env_str("APIGW_BIND") { cfg.bind = v; }
    if let Some(v) = env_u16("APIGW_PORT") { cfg.port = v; }
    if let Some(v) = env_str("APIGW_AUTH_HOST") { cfg.auth.host = v; }
    if let Some(v) = env_u16("APIGW_AUTH_PORT") { cfg.auth.port = v; }
    if let Some(v) = env_str("APIGW_PORTFOLIO_HOST") { cfg.portfolio.host = v; }
    if let Some(v) = env_u16("APIGW_PORTFOLIO_PORT") { cfg.portfolio.port = v; }
    if let Some(v) = env_str("APIGW_SETTLEMENT_HOST") { cfg.settlement.host = v; }
    if let Some(v) = env_u16("APIGW_SETTLEMENT_PORT") { cfg.settlement.port = v; }
    if let Some(v) = env_u64("APIGW_VERIFY_TIMEOUT_MS") { cfg.verify_timeout_ms = v; }
    if let Some(v) = env_u64("APIGW_PROXY_TIMEOUT_MS") { cfg.proxy_timeout_ms = v; }

    // CLI overrides (win over the environment).
    for a in &args {
        match a.name.as_str() {
            "bind" => cfg.bind = a.value.clone(),
            "port" => match a.value.parse::<u16>() {
                Ok(p) => cfg.port = p,
                Err(_) => {
                    eprintln!("api-gateway: --port must be a 16-bit integer, got {:?}", a.value);
                    process::exit(2);
                }
            },
            other => {
                eprintln!("api-gateway: unknown flag: --{}", other);
                process::exit(2);
            }
        }
    }

    // Validate; exit 1 on any config problem (logged, not panicked).
    if let Err(errs) = cfg.require_valid() {
        for e in &errs {
            eprintln!("api-gateway: config error: {}", e);
        }
        process::exit(1);
    }

    let bind = cfg.bind.clone();
    let port = cfg.port;
    let gateway = Gateway::new(cfg, Arc::new(HttpTransport));

    println!(
        "{} v{} starting on {}:{} (auth={}:{}, portfolio={}:{}, settlement={}:{})",
        SERVICE_NAME,
        SERVICE_VERSION,
        bind,
        port,
        gateway.config.auth.host,
        gateway.config.auth.port,
        gateway.config.portfolio.host,
        gateway.config.portfolio.port,
        gateway.config.settlement.host,
        gateway.config.settlement.port
    );

    match http::start(&bind, port, gateway) {
        Ok(handle) => {
            println!("listening on :{}", handle.port());
            handle.join();
        }
        Err(e) => {
            eprintln!("api-gateway: failed to start listener: {}", e);
            process::exit(1);
        }
    }
}
