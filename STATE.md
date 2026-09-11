# Project: HFT Platform Monorepo (`/home/user/hft-platform`)

**Goal:** 15 microservices (Python + Rust), fully implemented, no placeholders. Stdlib-only HTTP for Python services.

> **Session start:** read this file and `CONVENTIONS.md` only. `CONVENTIONS.md` §13
> (Agent Runtime Constraints) governs every session — write limits, read limits,
> bounded tool output, milestone checkpoints. Both files are kept lean (~31 KB total);
> this file is a STATUS file, not a history.
>
> **On-demand references (open only for a specific question, never in full):**
> - `.state/project-state.md` — detailed per-service inventory.
> - `.state/service-details.md` — per-service build history, fixes, design notes (S1–S15).
> - `.state/changelog-archive.md` — changelog rows older than the last 5 kept above.

## Service Plan

| ID | Service | Dir | Lang | Port | Status |
|----|---------|-----|------|------|--------|
| S1 | market-data-gateway | `services/market-data-gateway` | Python (mdg) | 7610 | ✅ COMPLETE & VERIFIED |
| S2 | order-book-builder | `services/order-book-builder` | Python (obb) | 7620 | ✅ COMPLETE & VERIFIED (13/13 tests pass) |
| S3 | strategy-engine | `services/strategy-engine` | Python (ste) | 7630 | ✅ COMPLETE & VERIFIED (20/20 tests pass) |
| S4 | execution-gateway | `services/execution-gateway` | Rust | 7640 | ✅ COMPLETE & VERIFIED (16/16 tests pass) |
| S5 | risk-manager | `services/risk-manager` | Python (rkm) | 7650 | ✅ COMPLETE & VERIFIED (27/27 tests pass) |
| S6 | position-keeper | `services/position-keeper` | Python (posk) | 7660 | ✅ COMPLETE & VERIFIED (33/33 tests pass) |
| S7 | latency-monitor | `services/latency-monitor` | Rust | 7670 | ✅ COMPLETE & VERIFIED (28/28 tests pass) |
| S8 | data-quality-monitor | `services/data-quality-monitor` | Python (dqm) | 7680 | ✅ COMPLETE & VERIFIED (38/38 tests pass) |
| S9 | portfolio-analytics | `services/portfolio-analytics` | Python (pfa) | 7690 | ✅ COMPLETE & VERIFIED (50/50 tests pass) |
| S10 | alerting-service | `services/alerting-service` | Rust | 7700 | ✅ COMPLETE & VERIFIED (40/40 tests pass) |
| S11 | config-service | `services/config-service` | Python (cfgs) | 7710 | ✅ COMPLETE & VERIFIED (31/31 tests pass) |
| S12 | auth-service | `services/auth-service` | Rust | 7720 | ✅ COMPLETE & VERIFIED (39/39 tests pass; live :7720 verified) |
| S13 | audit-logger | `services/audit-logger` | Python (audl) | 7730 | ✅ COMPLETE & VERIFIED (48/48 tests pass; live :7730 verified) |
| S14 | settlement-service | `services/settlement-service` | Python (stls) | 7740 | ✅ COMPLETE & VERIFIED (102/102 tests pass; live :7740 verified; +`--positions-url` flag, Session N) |
| S15 | api-gateway | `services/api-gateway` | Rust | 7750 | ✅ COMPLETE & VERIFIED (50 tests pass + 1 ignored: 33 service + 17 template KATs; live :7750 verified, 2026-09-11, Session L) |

Plus: `schemas/`, `deploy/`, `scripts/` population, and final `dependency-map.json` at repo root.

**Rust boilerplate:** `templates/rust/` holds verified, service-agnostic `json.rs`,
`router.rs`, `server.rs`, `crypto.rs`, and `API.md`. Scaffold every Rust service by
copying these (CONVENTIONS §7.1); never hand-roll or rewrite them, and read `API.md`
for their interfaces rather than reading the sources in full. They carry their own
`#[cfg(test)]` known-answer tests, which run automatically under `cargo test`.

## Original Intent (distilled)

- Generate a complete, production-grade HFT platform monorepo — 15 services, no placeholders/TODOs/elisions. Every function body must be fully implemented.
- Python services: **stdlib only** (no Flask/FastAPI/aiohttp/httpx). Rust services: **`std` only** (no external crates).
- Every service must be independently runnable (`python -m <pkg>.main` / `cargo run`) and testable (`pytest` / `cargo test`).
- All code must be verbose and fully implemented — as if written by a senior engineer who ships to production tomorrow. No "// rest of code", no "# TODO", no stubs.
- The deliverable is the **code on disk**, not a description of it.
- Final output includes `dependency-map.json` documenting the full network topology of all 15 services.
- Inter-service communication: HTTP/JSON only (no gRPC, no message queues). Streaming via chunked JSON-lines.

## Conventions

Binding rules live in `CONVENTIONS.md` (read it every session). §13 there — Agent
Runtime Constraints — governs write size, reads, tool-output limits, task
granularity, and milestone checkpoints, and overrides any habit inherited from an
earlier service. Do not maintain a second copy of the conventions here.

## Next Steps

All 15 services COMPLETE & VERIFIED. Phase 2 (items 1–5) COMPLETE: `schemas/` (Session M),
`deploy/` (N), `scripts/` (N), `dependency-map.json` finalized 20 edges (O), full-repo
verification sweep — test-all 15/15 + smoke PASS (P, 2026-09-11). `README.md` at repo root.

Pending: none. All 15 services and the dashboard tool (M1+M2+M3) are
COMPLETE & VERIFIED.

**State-file hygiene (in effect since 2026-09-11, Session Q):**
- STATE.md is a STATUS file — keep ≤ ~150 lines. Per-service build history, fixes,
  design notes → `.state/service-details.md` (archive; grep by service, never read in full).
- Changelog: keep only the last 5 rows here; older rows → `.state/changelog-archive.md`.
- Checkpoint updates: 1–3 lines in this file + 1 row in `.state/project-state.md` per event.

## Tools (not services — never add to the table above or to dependency-map services[])

| Tool | Dir | Port | Status |
|------|-----|------|--------|
| dashboard | `tools/dashboard` | 7760 | **M1+M2+M3 COMPLETE & VERIFIED** — 94/94 tests; M3: dark-theme SPA (`/` + `/static/*`), 6 panels, no-cache, whitelist + traversal-guarded static serving, read-only badge — spec in `tools/dashboard/PLAN.md` |

## Changelog

| Date | Change |
|------|--------|
| 2026-09-11 | **Tools: dashboard M3 COMPLETE & VERIFIED — tool done (M1+M2+M3).** Dark-theme SPA on :7760 (new `dash/staticfiles.py` + `src/dash/static/{index.html,dashboard.css,dashboard.js}`; `main.py` gained `_serve_index`/`_serve_static`, M1/M2 routes untouched): `GET /` → index.html (6 panel ids: health grid, portfolio, settlement, live, alerts & audit, gateway footer), `GET /static/*` whitelist {index.html, dashboard.css, dashboard.js} + `no-cache` + explicit content types; traversal/dot-segment/symlink-escape → UI-404 JSON (no disclosure); only 3 files ship, no build step. JS: vanilla ES2019, `textContent`-only DOM (no innerHTML), §1.2 envelope toasts, health poll 5 s, gateway-stats 10 s, EventSource on `/live/stream` (hello/degraded/recovered chips; alert & audit-event client-filtered), canvas sparkline; settle/finalize forms disabled + READ-ONLY badge from `/api/health` `read_only`. 13 new tests (`tests/test_static.py`: whitelist, traversal, symlink escape, types, panels, 404/405) → **94/94 pytest**. Live (15 svc S1 `--mock` + dashboard): 16/16 boot; `GET /` 200 text/html + 6 ids; css/js 200 correct types + no-cache; `/static/evil.css` + `--path-as-is /static/../main.py` → UI-404 JSON; POST `/` → 404 UI-404; SSE hello (6 subscribed) + quotes; `--read-only` boot: `read_only:true` in `/api/health`, POST settle → 403 UI-205, `GET /` still 200. Teardown: 7760 + 7610–7750 all free (16/16). |
| 2026-09-11 | **Tools: dashboard M2 COMPLETE & VERIFIED.** SSE `GET /live/stream` on :7760 (new `dash/live.py` + `dash/live_sources.py`, M1 untouched): 6 daemon pollers (mdg/obb/latmon/altsvc/cfgs/audl, 1.5 s read timeout) → bounded 1000-queue drop-oldest; envelope `{"src","kind","ts","data"}`; hello + 5 s keepalive; per-source dedup (mdg/obb seq, altsvc alert_id, audl hex event_id, latmon breach/recovery transitions, cfgs long-poll since-cursor — read timeout 5 s > 4 s watch); degraded/recovered announce-once; `?sources=`/`?symbol=`/`?max_age_s=` with 400 UI-202 pre-stream. 81/81 pytest (54 M1 + 27 new; injected clocks, fake transports — no sleeps). Live (15 svc, S1 --mock + dashboard): 16/16 boot; `curl -N --max-time 6` → hello (all 6 subscribed) + 13 mdg quotes + keepalive; UI-202 proven (bad sources, bad max_age_s); mid-stream obb kill → `degraded (URLError)`. Teardown: 7760 + 7610–7750 all free. |
| 2026-09-11 | **Tools: dashboard M1 COMPLETE & VERIFIED.** `tools/dashboard/` stdlib service :7760 (pkg `dash`): full M1+M2+M3 spec in PLAN.md; 54/54 pytest; live boot (15 svc + S1 `--mock` + dashboard) — 22/22 route checks (verbatim passthrough, UI-404/405/201/205, 15-port health sweep, gateway-stats wrapper), 401 auto-refresh proven via S12 `/revoke` (issued 1→2, 1 reactive refresh), read-only 403 no-upstream; teardown: 7760 + 7610–7750 all free. M2/M3 pending. |
| 2026-09-11 | **State files compressed (Session Q).** STATE.md demoted to lean STATUS file (124 KB → ~13 KB); S1–S15 detail sections → `.state/service-details.md`; older changelog rows → `.state/changelog-archive.md`; hygiene rule added (STATUS-only, last-5 changelog rows, on-demand archives). `README.md` added at repo root (204 lines). Stray empty `services/services/` removed. No code changes. |
| 2026-09-11 | **Phase 2 item 5 (Session P): full-repo verification sweep DONE.** `test-all.sh` 15/15 PASS (exit 0) + `smoke.sh` PASS (15/15 booted, healthz ok ×15, 15/15 killed, ports freed). **Phase 2 (items 1–5) complete.** |


