//! S12 auth-service — binary entrypoint.
//!
//! Boot sequence:
//! 1. Parse CLI flags: `--bind <addr>` (default `0.0.0.0`) and `--port <n>`
//!    (default `7720`). Unknown flags are boot errors (exit 2).
//! 2. Load the initial signing key from the environment:
//!    `AUTH_KEY_KID`, `AUTH_KEY_SECRET`, and/or `AUTH_KEY_SECRET_HEX`.
//!    When no secret is provided at all, a fully random key is generated.
//! 3. Build `AuthConfig` and call `require_valid()` — exits(1) on any problem.
//! 4. Construct the `AuthManager` (production `SystemClock`) and start the
//!    std-only HTTP server.
//! 5. Block on the server handle for the lifetime of the process.

use std::env;
use std::process;

use authsvc::config::{AuthConfig, SERVICE_NAME, SERVICE_VERSION, DEFAULT_PORT};
use authsvc::core::{AuthManager, Clock, SystemClock};
use authsvc::crypto;
use authsvc::http;
use authsvc::models::SigningKey;

/// Default bind address when `--bind` is not given.
const DEFAULT_BIND: &str = "0.0.0.0";

/// Current time in int64 ns since the Unix epoch, via the production clock.
fn now_ns() -> i64 {
    SystemClock.now_ns()
}

/// Parsed CLI flags.
struct Flags {
    bind: String,
    port: u16,
}

/// Parse `--bind` / `--port` (both `--flag value` and `--flag=value` forms)
/// from the raw argument list; anything unrecognized is a boot error.
fn parse_args(args: &[String]) -> Result<Flags, String> {
    let mut bind = DEFAULT_BIND.to_string();
    let mut port: Option<u16> = None;
    let mut i = 1; // skip argv[0]
    while i < args.len() {
        let a = &args[i];
        if a == "--bind" {
            i += 1;
            if i >= args.len() {
                return Err("missing value for --bind".to_string());
            }
            bind = args[i].clone();
        } else if a == "--port" {
            i += 1;
            if i >= args.len() {
                return Err("missing value for --port".to_string());
            }
            let raw = args[i].clone();
            port = Some(raw.parse::<u16>().map_err(|_| format!("invalid --port value: {raw}"))?);
        } else if let Some(rest) = a.strip_prefix("--bind=") {
            bind = rest.to_string();
        } else if let Some(rest) = a.strip_prefix("--port=") {
            port = Some(rest.parse::<u16>().map_err(|_| format!("invalid --port value: {rest}"))?);
        } else {
            return Err(format!("unknown argument: {a}"));
        }
        i += 1;
    }
    Ok(Flags { bind, port: port.unwrap_or(DEFAULT_PORT) })
}

/// Load the initial signing key from the environment, or generate a random one.
///
/// Environment variables (all optional):
///   AUTH_KEY_KID         — explicit key id (default: generated `kid-<random>`)
///   AUTH_KEY_SECRET      — raw secret bytes as a plain string
///   AUTH_KEY_SECRET_HEX  — secret as a hex string; wins over AUTH_KEY_SECRET
/// If no secret is set at all, a fully random key (random kid + 32-byte secret)
/// is generated so the service is always bootable standalone.
fn load_initial_key() -> Result<SigningKey, String> {
    let now = now_ns();
    let hex_secret = env::var("AUTH_KEY_SECRET_HEX").ok().filter(|s| !s.trim().is_empty());
    let raw_secret = env::var("AUTH_KEY_SECRET").ok().filter(|s| !s.trim().is_empty());
    let kid = env::var("AUTH_KEY_KID").ok().filter(|s| !s.trim().is_empty());

    if hex_secret.is_none() && raw_secret.is_none() {
        return SigningKey::generate(now).map_err(|e| format!("failed to generate signing key: {e}"));
    }

    let secret: Vec<u8> = if let Some(h) = hex_secret {
        crypto::hex_decode(&h).map_err(|e| format!("AUTH_KEY_SECRET_HEX is invalid: {e}"))?
    } else {
        raw_secret.expect("checked above").into_bytes()
    };
    if secret.is_empty() {
        return Err("signing key secret must not be empty".to_string());
    }

    let kid = match kid {
        Some(k) => k,
        None => {
            let suffix = crypto::random_token(8).map_err(|e| format!("failed to generate key id: {e}"))?;
            format!("kid-{suffix}")
        }
    };
    Ok(SigningKey::new(&kid, secret, now))
}

fn main() {
    let raw: Vec<String> = env::args().collect();
    let flags = match parse_args(&raw) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("{SERVICE_NAME} usage error: {e}");
            process::exit(2);
        }
    };

    let mut cfg = AuthConfig::default();
    cfg.port = flags.port;
    cfg.require_valid();

    let key = match load_initial_key() {
        Ok(k) => k,
        Err(e) => {
            eprintln!("{SERVICE_NAME} boot error: {e}");
            process::exit(1);
        }
    };
    let port = cfg.port;

    let key_kid = key.kid.clone();
    let mgr = std::sync::Arc::new(AuthManager::new(cfg, vec![key]));

    println!(
        "{SERVICE_NAME} v{SERVICE_VERSION} starting on {bind}:{port} (key={kid}, ring_size=1)",
        bind = flags.bind,
        kid = key_kid,
    );

    let handle = match http::start(&flags.bind, port, mgr) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("{SERVICE_NAME} failed to bind {bind}:{port}: {e}", bind = flags.bind);
            process::exit(1);
        }
    };
    println!("{SERVICE_NAME} listening on :{}", handle.port());

    // Block the main thread for the lifetime of the server.
    handle.join();
}
