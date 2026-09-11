# HFT Platform

A complete, production-grade **high-frequency trading platform monorepo**: 15
microservices (10 Python, 5 Rust), each independently runnable and testable,
communicating exclusively over **HTTP/JSON** (streaming via chunked JSON-lines,
long-poll where useful), plus one **operations dashboard** tool on :7760. No
external dependencies anywhere:

- **Python services** — standard library only (no Flask/FastAPI/httpx).
  HTTP is `http.server.ThreadingHTTPServer` with a custom handler + minimal
  `{param}` router.
- **Rust services** — `std` only (zero crates). The JSON codec, threaded
  `TcpListener` HTTP server, and SHA-256/HMAC/base64url crypto are
  hand-rolled, shared as byte-identical templates (`templates/rust/`), and
  carry their own known-answer tests.

On top of the 15 services there is a single **tool** (`tools/dashboard`, pkg
`dash`, port 7760) that is *not* a service: it proxies S15, sweeps health, and
aggregates a live SSE stream + a dark-theme SPA for operators. It never appears
in the 15-service table or `dependency-map.json`.

Every service is version `1.0.0`, speaks the same error envelope (§1.2 of
`CONVENTIONS.md`), exposes `GET /healthz` and `GET /readyz`, and is verified:
full test suite green, plus a live boot on its assigned port with every
endpoint exercised.

## The 15 services

| ID  | Service                 | Dir                     | Lang   | Port | Error prefix | Role |
|-----|-------------------------|-------------------------|--------|------|--------------|------|
| S1  | market-data-gateway     | `services/market-data-gateway` | Python (`mdg`)  | 7610 | `MDG-` | Ingests venue feeds (socket transport; `--mock` for offline deterministic feeds), normalizes quotes/trades, sharded ring buffers, feed-quality tracking |
| S2  | order-book-builder      | `services/order-book-builder`  | Python (`obb`)  | 7620 | `OBB-` | Maintains L2 order books from S1 quotes (N/M/D/E/B/O actions), cross detection, top-of-book views, material-change events, snapshot rebuilds |
| S3  | strategy-engine         | `services/strategy-engine`     | Python (`ste`)  | 7630 | `STE-` | Pluggable strategies (momentum / mean-reversion / spread-arb) over S2 top-of-book; signals → risk-capped order intents pushed to S4 |
| S4  | execution-gateway       | `services/execution-gateway`   | Rust (`exg`)    | 7640 | `EXG-` | Order lifecycle (NEW → PARTIALLY_FILLED/FILLED/CANCELED/REJECTED), simulated venue decisions + fill-at-limit, order/fill queries |
| S5  | risk-manager            | `services/risk-manager`        | Python (`rkm`)  | 7650 | `RKM-` | Pre-trade veto pipeline (kill-switch → sanity → velocity → position/notional caps), SOFT/HARD breaches, kill-switch engage/disengage, real-time exposure |
| S6  | position-keeper         | `services/position-keeper`     | Python (`posk`) | 7660 | `POS-` | Authoritative (account, symbol) position ledger; exactly-once S4 fill ingestion, average-cost accounting, corporate actions, snapshots |
| S7  | latency-monitor         | `services/latency-monitor`     | Rust (`latmon`) | 7670 | `LAT-` | Per-stage pipeline latency (5 stages), integer-ns percentiles (p50/p99/p999), budget-breach alerts with cooldown |
| S8  | data-quality-monitor    | `services/data-quality-monitor`| Python (`dqm`)  | 7680 | `DQM-` | Composite per-symbol quality score with hysteresis, gap/staleness tracking, degradation episodes, S10 alert fan-out |
| S9  | portfolio-analytics     | `services/portfolio-analytics` | Python (`pfa`)  | 7690 | `PFA-` | P&L (aggregate + per-symbol), Sharpe/drawdown/win-rate/annualized, attribution, historical + parametric VaR/CVaR over the S6 book |
| S10 | alerting-service        | `services/alerting-service`    | Rust (`altsvc`) | 7700 | `ALT-` | Central alert aggregation: dedup/suppression, lazy severity escalation, ack (freezes escalation), resolve, bounded dispatch log |
| S11 | config-service          | `services/config-service`      | Python (`cfgs`) | 7710 | `CFG-` | Versioned per-service config blobs (SHA-256 content hash), env overrides deep-merged at read time, feature flags, sequenced change log + long-poll watch |
| S12 | auth-service            | `services/auth-service`        | Rust (`authsvc`)| 7720 | `AUT-` | HS256 JWT issue/verify/revoke (in-crate HMAC-SHA256), key ring with rotation, bounded revocation set, JWKS-style public key view |
| S13 | audit-logger            | `services/audit-logger`        | Python (`audl`) | 7730 | `AUD-` | Immutable, hash-chained (SHA-256 over canonical JSON) append-only audit trail; dedup, bounded eviction with re-severance, full-chain verification |
| S14 | settlement-service      | `services/settlement-service`  | Python (`stls`) | 7740 | `STL-` | End-of-day settlement: idempotent fill settlement, venue-statement reconciliation, netting + cash roll-ups, tamper-evident EOD seal |
| S15 | api-gateway             | `services/api-gateway`         | Rust (`apigw`)  | 7750 | `API-` | External-facing REST gateway: authenticates via S12, authorizes route scopes, reverse-proxies `/portfolio/**` → S9 and `/settlement/**` → S14 with path rewrite |

## Data flow (happy path)

```
venues ──► S1 ──► S2 ──► S3 ──► S4 ──► fills
              │       │      │        │
              │       │      └──► S5 (pre-trade veto, kill-switch)
              │       └──► S8 (quality)              │
              └──► S8                               ▼
                                      S6 (positions) ──► S9 (analytics)
                                               │
                          S5/S11 ──► S13 (audit) ◄── S14 (settlement ◄─ S6)
        any service ──► S10 (alerts)      S12 (auth) ──► S15 (gateway) ──► S9/S14
        S11 (config) serves config + flags to the platform at boot/change
```

The authoritative inter-service topology (20 edges, every caller/callee pair
derived from actual client code) lives in `dependency-map.json` at the repo
root, validated by `schemas/dependency-map.schema.json`.

## Repository layout

```
hft-platform/
├── README.md                  # this file
├── CONVENTIONS.md             # binding rules for all 15 services (read this first)
├── STATE.md                   # per-service build/verification history + changelog
├── TASKS.md                   # original task brief
├── INSTRUCTIONS.md            # session instructions
├── dependency-map.json        # canonical service topology (20 edges) + notes
├── .state/                    # detailed project inventory (per-service notes)
├── services/                  # the 15 services (see table above)
│   └── <service>/
│       ├── pyproject.toml     # Python: src/<pkg>/ layout, stdlib-only
│       ├── src/…              # Python package or Rust src/ (lib + bin, zero crates)
│       └── tests/             # pytest suites / Rust integration tests by area
├── schemas/                   # 21 JSON schemas: per-service API schemas + wire formats
├── deploy/                    # docker-compose (15 services) + 2 shared Dockerfiles
│   ├── docker-compose.yml
│   ├── docker/                #   Dockerfile.python, Dockerfile.rust
│   ├── env.example
│   └── README.md
├── scripts/                   # repo-level tooling (POSIX sh, stdlib-only probing)
│   ├── build-all.sh           #   byte-compile 10 Python pkgs + cargo build 5 crates
│   ├── test-all.sh            #   run all 15 test suites, PASS/FAIL table
│   ├── smoke.sh               #   boot all 15 on 7610–7750, healthz sweep, kill
│   └── kill-all.sh            #   /proc-based process scan + port-free verification
├── tools/
│   └── dashboard/             # operations dashboard (tool, not a service, :7760)
│       ├── PLAN.md            #   full M1 (proxy) + M2 (SSE) + M3 (SPA) spec
│       ├── README.md          #   run/endpoints/test reference
│       ├── src/dash/          #   stdlib pkg: proxy, live stream, static SPA
│       └── tests/             #   94-test pytest suite
└── templates/rust/            # shared Rust boilerplate: json/router/server/crypto
                               #   (+ API.md — copy, never rewrite, never read in full)
```

## Prerequisites

- **Python 3.12** (stdlib only for the services themselves; `pytest` must be
  importable to run the test suites).
- **Rust toolchain** (rustup, stable ≥ 1.80). No crates are ever fetched —
  every Rust build works `--offline`.
- POSIX `sh`. The tooling avoids bashisms and relies on no special binaries:
  port probing is done with Python `socket.create_connection` (no `ss`/`lsof`)
  and process discovery with `/proc/<pid>/cmdline` scans (no `pgrep`).

## Quickstart — one service

Python service (run from inside the service directory):

```sh
cd services/risk-manager
PYTHONPATH=src python3 -m rkm.main --port 7650        # serves /healthz, /readyz, …
```

Rust service:

```sh
cd services/execution-gateway
export PATH="$HOME/.cargo/bin:$PATH"
cargo run                                            # lib + bin, zero crates
./target/debug/execution-gateway                     # port 7640 by default
```

S12 and S15 also accept `--bind 127.0.0.1`; S1 accepts `--mock` for
deterministic offline venue feeds (what `smoke.sh` and compose use). The only
functional environment variables on the platform are S12's `AUTH_KEY_*`
(signing key) and S15's `APIGW_*` (upstream URLs/timeouts) — see
`deploy/env.example` for the full inventory. Python services read no env.

Run every service's test suite in one shot (15 suites, bounded output,
PASS/FAIL table, non-zero exit on any failure):

```sh
sh scripts/test-all.sh     # → "test-all: all 15 suites PASS"
```

Current verified totals: Python 6+13+20+27+33+38+50+31+48+102 = 368 tests green, plus
(mdg/obb/ste/rkm/posk/dqm/pfa/cfgs/audl/stls) and Rust per-crate
`cargo test` green (exg 16, latmon 28, altsvc 40, authsvc 39+1 ignored,
apigw 50+1 ignored — service tests + shared template KATs; the ignored tests
are the templates' live-HTTP checks).

## Run the whole platform

**Option A — Docker Compose (recommended):**

```sh
cd deploy
docker compose up --build        # all 15 services, ports 7610–7750, TCP healthchecks
docker compose ps                # healthy ×15
```

The compose file mirrors the dependency-map edges in `depends_on` (a
superset, for boot ordering), and S1 runs `mdg.main --mock` so the whole
platform is deterministic offline. S14 is pointed at S6 via
`--positions-url http://position-keeper:7660`. See `deploy/README.md` for
networking and gotchas.

**Option B — host processes (no Docker):** boot in dependency order, e.g.

```sh
sh scripts/smoke.sh      # boots all 15 on their real ports, sweeps /healthz,
                         # then kills everything and verifies 15/15 ports freed
```

`smoke.sh` is both the verification tool and a working demo: its last lines
read `smoke: PASS — 15/15 booted, healthz ok, 15/15 killed, all ports free`.
`sh scripts/kill-all.sh` tears down anything the platform left behind.

To leave the platform running on the host, start services in edge order
(S1 → S2/S7 → S3 → S4 → S5/S6 → …, S10/S11/S12/S13 available early), each as
shown in the quickstart; S15 last.

## Conventions every service obeys

The full binding rules are in `CONVENTIONS.md`; the essentials:

- **Error envelope (all 15 services):**
  ```json
  { "error": { "code": "MDG-001", "message": "…", "service": "market-data-gateway",
              "retryable": true, "context": { "…": "…" } } }
  ```
  Codes are `PREFIX-NNN` (per-service prefix, §1.1 table in CONVENTIONS.md);
  4xx-class request errors carry `retryable:false`, transport/upstream
  failures `true`.
- **Timestamps:** int64 nanoseconds since Unix epoch on all hot paths; no
  float latency math; wire keys `vt`/`rt` (venue/receive). Market-data wire
  formats are specified in CONVENTIONS §4 and machine-checked by
  `schemas/*.schema.json`.
- **Health:** `GET /healthz` → `{"status":"ok","service":"…","version":"1.0.0"}`;
  `GET /readyz` → `{"status":"ready"|"not_ready","reasons":[…]}` (readiness
  reflects upstream ingest health, not liveness).
- **Config:** frozen dataclasses (Python) / immutable structs (Rust) + a
  `validate()` run once at boot; failure exits 1. Centralized distribution is
  S11's job at deploy time, not each service's.
- **Tests:** no network beyond loopback, no sleeping — time-based behaviour
  (expiry, escalation, dedup windows) is driven by an injected `ManualClock`.

## Where to look next

| Question | File |
|----------|------|
| What must every service do? | `CONVENTIONS.md` (incl. §13 agent constraints) |
| History of every build/verification milestone | `STATE.md` (changelog at the bottom) |
| Detailed per-service inventory | `.state/project-state.md` |
| Who calls whom? | `dependency-map.json` (+ its schema in `schemas/`) |
| Service request/response shapes | `schemas/services/*.schema.json` |
| Docker details (images, env, networking) | `deploy/README.md`, `deploy/env.example` |
| Rust boilerplate interfaces | `templates/rust/API.md` |
| Operations dashboard (proxy, SSE, SPA) | `tools/dashboard/README.md` (+ `tools/dashboard/PLAN.md`) |
