//! alerting_service — service entrypoint.
//!
//! Boot sequence:
//!
//! 1. Build and validate the configuration (abort on failure).
//! 2. Construct the shared :struct:`AlertManager`.
//! 3. Start the std-only HTTP server thread.
//! 4. Block on the main thread until a termination signal arrives.

use std::time::Duration;

fn main() {
    let cfg = altsvc::config::Config::default();
    let errors = cfg.validate();
    if !errors.is_empty() {
        eprintln!("configuration invalid: {}", errors.join("; "));
        std::process::exit(1);
    }

    println!(
        "starting {} v{} (env={}) on :{}",
        cfg.name, cfg.version, cfg.env, cfg.listen_port
    );

    let manager = std::sync::Arc::new(altsvc::core::AlertManager::new(cfg.clone()));
    let router = altsvc::router::Router::with_default_routes();

    match altsvc::http::serve(cfg.listen_port, router, manager) {
        Ok(port) => println!("HTTP API listening on :{port}"),
        Err(e) => {
            eprintln!("failed to bind :{}: {e}", cfg.listen_port);
            std::process::exit(1);
        }
    }

    // Block until interrupted (Ctrl-C / SIGTERM).  A simple sleep loop keeps the
    // process alive without any external signal-handling dependency.
    loop {
        thread_sleep(Duration::from_secs(1));
    }
}

fn thread_sleep(d: Duration) {
    std::thread::sleep(d);
}
