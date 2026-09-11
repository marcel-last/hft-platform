# HFT Platform — Deployment

Docker Compose deployment for all 15 microservices (ports **7610–7750**).

```
deploy/
├── docker-compose.yml          # 15 services, ports, depends_on, healthchecks
├── docker/
│   ├── Dockerfile.python       # shared image for the 10 Python (stdlib-only) services
│   └── Dockerfile.rust         # shared image for the 5 Rust (std-only) services
├── env.example                 # complete environment-variable reference
└── README.md                   # this file
```

There is also a **repo-root** `.dockerignore` (build contexts are the repo root,
so the ignore file must sit there).

## Quick start

```sh
cd deploy
docker compose up --build          # build all 15 images, start the platform
docker compose up market-data-gateway -d --build   # or one service at a time
docker compose down                # stop everything
```

Every service is healthy when its TCP port accepts connections (each compose
entry has a `healthcheck`; verify with `docker compose ps`). The external entry
point is the **api-gateway on :7750**; all other ports are internal service APIs
(also published to the host for direct access / debugging).

## Service → port → image map

| Service | Dir | Image build | Port | Notes |
|---------|-----|-------------|------|-------|
| S1 market-data-gateway | `services/market-data-gateway` | `Dockerfile.python` (PKG `mdg`) | 7610 | runs with `--mock` (deterministic offline venue feeds) |
| S2 order-book-builder | `services/order-book-builder` | `Dockerfile.python` (PKG `obb`) | 7620 | |
| S3 strategy-engine | `services/strategy-engine` | `Dockerfile.python` (PKG `ste`) | 7630 | |
| S4 execution-gateway | `services/execution-gateway` | `Dockerfile.rust` | 7640 | takes no CLI args |
| S5 risk-manager | `services/risk-manager` | `Dockerfile.python` (PKG `rkm`) | 7650 | |
| S6 position-keeper | `services/position-keeper` | `Dockerfile.python` (PKG `posk`) | 7660 | |
| S7 latency-monitor | `services/latency-monitor` | `Dockerfile.rust` | 7670 | takes no CLI args |
| S8 data-quality-monitor | `services/data-quality-monitor` | `Dockerfile.python` (PKG `dqm`) | 7680 | |
| S9 portfolio-analytics | `services/portfolio-analytics` | `Dockerfile.python` (PKG `pfa`) | 7690 | |
| S10 alerting-service | `services/alerting-service` | `Dockerfile.rust` | 7700 | takes no CLI args |
| S11 config-service | `services/config-service` | `Dockerfile.python` (PKG `cfgs`) | 7710 | |
| S12 auth-service | `services/auth-service` | `Dockerfile.rust` | 7720 | auto-generates signing key unless `AUTH_KEY_*` set |
| S13 audit-logger | `services/audit-logger` | `Dockerfile.python` (PKG `audl`) | 7730 | |
| S14 settlement-service | `services/settlement-service` | `Dockerfile.python` (PKG `stls`) | 7740 | `--positions-url http://position-keeper:7660` |
| S15 api-gateway | `services/api-gateway` | `Dockerfile.rust` | 7750 | external entry point |

## How the shared Dockerfiles work

Both Dockerfiles are parameterized by `--build-arg SERVICE=<service-dir>`
(compose passes it per service):

- **`Dockerfile.python`** — `python:3.12-slim`, copies the one service's
  `src/` + `pyproject.toml` to `/app`, sets `PYTHONPATH=/app/src`, and runs
  `python -m ${PKG}.main` (PKG baked in from a second build arg). No pip
  installs: the platform is Python **stdlib only**.
- **`Dockerfile.rust`** — `rust:1.80-slim` builder compiles the one crate with
  `cargo build --release --offline` (zero external crates, pinned `Cargo.lock`),
  then copies the single binary into `debian:bookworm-slim`. No glibc extras,
  no CA bundle — the platform is plain HTTP.

Both set `STOPSIGNAL SIGTERM` — every service has a SIGTERM clean-shutdown
hook, so `docker stop` triggers it (Python mains log `shutting down …`; Rust
mains exit after the server loop is joined).

## Networking & service discovery

Compose service names **are** the DNS names, and they intentionally match the
`services/` directory names: the Python services' built-in upstream URLs are
compose-style defaults (e.g. `http://order-book-builder:7620`), so a stock
`docker compose up` works with **no** per-service URL configuration. Two
exceptions are handled in the compose file itself:

- **S1 → venue feeds**: `market-data-gateway` runs with `--mock` so the platform
  is fully self-contained offline (deterministic seeded replay). For live venues
  drop the flag and provide venue transport configuration.
- **S14 → S6 ingest**: `settlement-service`'s S6 pull URL defaults to
  `http://127.0.0.1:7660` (host-mode); compose overrides it via
  `--positions-url http://position-keeper:7660`.

`depends_on` encodes the `dependency-map.json` edges as startup ordering only
(compose v2 has no health-gated depends); every service tolerates upstream
outage by design (bounded backoff, `readyz` reflects the gap, no crash loop —
`restart: unless-stopped`).

## Environment variables

Only **S12** (`AUTH_KEY_KID` / `AUTH_KEY_SECRET` / `AUTH_KEY_SECRET_HEX`) and
**S15** (`APIGW_*`) read env vars at runtime — see `env.example` for the full
documented list with all defaults. Per CONVENTIONS §5 the Python services read
**no** env vars; their behaviour is configured via frozen dataclass defaults
(overridable through config-service S11 or `config.py`).

## Testing in this environment

- Host test suites (no Docker needed): `scripts/test-all.sh` (see `scripts/`).
- Docker image builds need Docker; this repo's CI-less baseline verifies the
  same binaries the images package (`cargo build --release` output is the
  copied artifact; `python -m <pkg>.main` is the image CMD).

## Gotchas

- Rust images: S4/S7/S10 binaries accept **no** CLI arguments (bind 0.0.0.0 at
  their configured port); S12/S15 accept `--bind`/`--port` (compose passes
  them).
- S12 auto-generates a fresh signing key on every container start unless you
  set `AUTH_KEY_*` (see the commented `environment:` block in compose).
- Ports are published `127.0.0.1`-independent (`"7610:7610"` binds all
  interfaces on the host). For a private network, change to `"127.0.0.1:7610:7610"`.
- `docker compose down` does **not** clean up volumes; this platform keeps no
  stateful volumes (all state is in-memory by design).
