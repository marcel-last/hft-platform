# tools/dashboard — Full Plan (M1 + M2 + M3)

> **Single source of truth for the dashboard tool.** Later sessions read this file
> plus `STATE.md` / `CONVENTIONS.md` — nothing else is needed.
>
> The dashboard is a **tool, NOT service #16**: it never appears in STATE.md's
> 15-service table and never in `dependency-map.json`'s `services[]`. It is tracked
> in STATE.md's "## Tools" table and `.state/project-state.md` only.
>
> Stack: **Python stdlib only** (no third-party deps), `ThreadingHTTPServer`,
> package `dash` under `tools/dashboard/src/`. Port **7760**.

## 0. Milestone status

| Milestone | Scope | Status |
|-----------|-------|--------|
| M1 | Backend API proxy (`/api/*`) + auth token mgmt + health sweep | see STATE.md Tools table |
| M2 | SSE live stream `GET /live/stream` (multi-source poller aggregator) | ✅ COMPLETE & VERIFIED (2026-09-11, Session S) — `dash/live.py` + `dash/live_sources.py`; 81/81 tests; live-proven incl. mid-stream kill → degraded |
| M3 | Dark-theme single-page UI (`/` + `static/*`) | ✅ COMPLETE & VERIFIED (2026-09-11) — `dash/staticfiles.py` + `src/dash/static/` (index.html, dashboard.css, dashboard.js); 94/94 tests; live-proven incl. `--read-only` badge + UI-205 on write |

## 1. M1 — Backend API proxy (built)

### 1.1 Package layout

```
tools/dashboard/
├── pyproject.toml            # name "hft-dashboard", version 1.0.0, no deps
├── README.md                 # run instructions
├── src/dash/
│   ├── __init__.py           # re-exports
│   ├── config.py             # frozen dataclasses + validate_config + CONFIG
│   ├── errors.py             # UI-NNN exception hierarchy + error_envelope()
│   ├── router.py             # regex Router with {param} (CONVENTIONS §3 style)
│   ├── clients.py            # AuthClient (token mgmt) + GatewayClient (proxy)
│   ├── controller.py         # one function per /api/* route
│   # (the BaseHTTPRequestHandler bridge lives in main.py — there is no separate handlers.py)
│   └── main.py               # CLI (--bind/--port/--auth-url/--gateway-url/--read-only), boot
└── tests/
    ├── test_router.py        # route resolution, params, 404/405
    ├── test_errors.py        # envelope shape, code classes
    ├── test_token.py         # token fetch/refresh/exhaustion (fake transports)
    ├── test_clients.py       # passthrough verbatim, 401-retry, read-only gate
    ├── test_controller.py    # route table mapping, health sweep (fake clients)
    └── test_handlers.py      # (status, body) tuple contract, body parse errors
```

### 1.2 CLI / config

`python3 -m dash.main` (from `tools/dashboard`, `PYTHONPATH=src`):

| Flag | Default | Meaning |
|------|---------|---------|
| `--bind` | `0.0.0.0` | listen address |
| `--port` | `7760` | listen port |
| `--auth-url` | `http://127.0.0.1:7720` | S12 auth-service base URL |
| `--gateway-url` | `http://127.0.0.1:7750` | S15 api-gateway base URL |
| `--read-only` | off | refuse write-scope routes with 403 UI-205 |

`config.py`: frozen `AuthConfig(sub="dashboard", scopes=("read","write"),
token_ttl_ns=3_600_000_000_000)` (1 h), `GatewayConfig`, `HealthConfig`
(service table: 15 (port, name, package) pairs, probe timeout 800 ms),
`DashConfig` root with `validate_config()`. Token TTL is exactly 1 hour
= `3_600_000_000_000_000` ns, passed to S12 as `ttl_ns`.

### 1.3 Auth token management (`clients.py`)

- `AuthClient` posts `POST {auth}/token` with
  `{"sub": "dashboard", "scopes": ["read", "write"], "ttl_ns": 3.6e12}`.
  S12 replies (200): `{"token","kid","jti","sub","scope":[...],"iat","nbf","exp"}`.
- Token cache: single `(token, jti, exp_ns)` under a `threading.Lock`.
  **Proactive refresh**: when remaining TTL < 25 % of the original TTL the next
  `get_token()` re-fetches. **Reactive refresh**: any gateway **401** response →
  drop cache, re-fetch ONCE, retry the request exactly once. A second 401 is
  passed through verbatim (the request is answered with the gateway's 401 body).
  If the re-fetch itself fails → 502 UI-403 (`retryable: true`).
- Auth-service request stats counted locally: `tokens_issued`, `token_refreshes`
  (reactive), exposed in `GET /api/gateway-stats` under `dashboard.stats`.

### 1.4 Route table (dashboard → S15 → upstream)

Query strings are forwarded **verbatim** to the gateway; S15/S9/S14 validate and
answer with their own `PREFIX-NNN` envelopes on bad params.

| Dashboard route | Method | Gateway path | Scope | Upstream |
|-----------------|--------|--------------|-------|----------|
| `/api/portfolio/pnl` | GET | `/portfolio/pnl` | read | S9 :7690 |
| `/api/portfolio/pnl/{symbol}` | GET | `/portfolio/pnl/{symbol}` | read | S9 |
| `/api/portfolio/metrics` | GET | `/portfolio/metrics` | read | S9 |
| `/api/portfolio/var` | GET | `/portfolio/var` | read | S9 |
| `/api/portfolio/attribution` | GET | `/portfolio/attribution` | read | S9 |
| `/api/portfolio/history` | GET | `/portfolio/history` | read | S9 |
| `/api/settlement/settle` | POST | `/settlement/settle` | **write** | S14 :7740 |
| `/api/settlement/finalize/{date}` | POST | `/settlement/finalize/{date}` | **write** | S14 |
| `/api/settlement/ingest` | POST | `/settlement/ingest` | **write** | S14 |
| `/api/settlement/reports/{date}` | GET | `/settlement/reports/{date}` | read | S14 |
| `/api/settlement/runs/{date}` | GET | `/settlement/runs/{date}` | read | S14 |
| `/api/settlement/discrepancies` | GET | `/settlement/discrepancies` | read | S14 |
| `/api/health` | GET | (none — local sweep, §1.5) | — | all 15 |
| `/api/gateway-stats` | GET | `/stats` | read | S15 |

- `--read-only`: the three write routes answer **403 UI-205** before any upstream
  call (no token spent); `GET /api/health` reports `read_only: true`.
- **Passthrough rule**: gateway/upstream responses (any status) are returned
  **verbatim** — same status code, same JSON body (their envelope untouched).
  The dashboard never rewrites an upstream envelope.
- `GET /api/gateway-stats` = verbatim S15 `/stats` body
  (`requests_total, auth_ok, auth_rejected, scope_denied, auth_upstream_errors,
  route_hits, route_404, route_405, proxied, proxy_upstream_errors`) plus a
  dashboard-local wrapper: `{"stats": <verbatim>, "dashboard": {"read_only": bool,
  "tokens_issued": N, "token_refreshes": N, "now_ns": ...}}`.

### 1.5 `GET /api/health` — 15-port sweep (one call)

Probes all 15 services (7610…7750 step 10) **in parallel** (one daemon thread
per port, each with an 800 ms connect+read timeout) hitting both `GET /healthz`
and `GET /readyz` on each port. Response:

```json
{"services": [
   {"name": "market-data-gateway", "port": 7610, "live": true,
    "healthz": "ok", "healthz_version": "1.0.0",
    "ready": "ready", "ready_reasons": [], "latency_ms": 3, "error": null},
   ...
], "summary": {"live": 14, "down": 1, "not_ready": 1, "checked": 15,
               "elapsed_ms": 42, "read_only": false}}
```

`healthz` is `"ok"|"error"|"unreachable"`; a live-but-not-ready service counts as
`live: true, ready: "not_ready"`. Always answers 200 (it is a probe, not a proxy).

### 1.6 Error codes (dashboard's own, `UI-NNN`; envelope per CONVENTIONS §1.2)

| Code | HTTP | Meaning | retryable |
|------|------|---------|-----------|
| UI-001 | 500 | unexpected internal error | false |
| UI-201 | 400 | malformed/missing JSON request body | false |
| UI-202 | 400 | unsupported value in dashboard-local query param | false |
| UI-205 | 403 | write route called while `--read-only` | false |
| UI-401 | 502 | auth-service unreachable (token fetch failed) | true |
| UI-402 | 502 | gateway transport failure (connect/read timeout) | true |
| UI-403 | 502 | token re-fetch failed after gateway 401 | true |
| UI-404 | 404 | unknown dashboard route | false |
| UI-405 | 405 | method not allowed on known route | false |

`service` field is always `"dashboard"`.

### 1.7 Local endpoints (no gateway)

- `GET /healthz` → `{"status":"ok","service":"dashboard","version":"1.0.0"}`
- `GET /readyz` → ready when the last token fetch succeeded; `reasons` include
  `"token-not-acquired"` otherwise. 200 in both states.

### 1.8 M1 verification (recorded in `.state/project-state.md`)

1. `python3 -` socket probe confirms :7760 free before boot.
2. Boot platform (README Option B host mode; S1 with `--mock`) + dashboard.
3. `curl` every `/api/*` route (13 proxy routes + health sweep + stats);
   assert verbatim upstream bodies on 200 and envelope passthrough on 4xx.
4. Prove 401 auto-refresh: note S12 `/stats` `issued` counter → revoke the
   dashboard's current `jti` via `POST /revoke` → next dashboard call must
   answer 200 (reactive re-fetch) and S12 `issued` must have incremented.
5. `sh scripts/kill-all.sh`; re-probe: 7760 + 7610–7750 all free.

## 2. Testing conventions (M1)

pytest-compatible plain `assert` files under `tests/`, run via
`cd tools/dashboard && python -m pytest tests/ -q`. **No network in unit tests**:
`clients.py` takes injectable transport callables (`_transport` on
`AuthClient`/`GatewayClient`); `controller.py` takes injected client objects.
Time-dependent token logic is tested with an injected `now_ns` callable
(ManualClock pattern per CONVENTIONS §10), never by sleeping.

## 3. M2 — SSE live stream (`GET /live/stream`)

### 3.1 Purpose

One `text/event-stream` response aggregating "interesting" platform events from
the six sources that have queryable event/state endpoints:
S1 market-data-gateway (:7610), S2 order-book-builder (:7620), S7
latency-monitor (:7670), S10 alerting-service (:7700), S11 config-service
(:7710), S13 audit-logger (:7730).

### 3.2 Wire format

`Content-Type: text/event-stream`, `Cache-Control: no-cache`,
`X-Accel-Buffering: no`, chunked. One `data: {json}\n\n` event per source event.
Event envelope (uniform, source-tagged):

```json
{"src": "mdg", "kind": "quote", "ts": 1788940747832091143, "data": { ...raw upstream object... }}
```

- `src` ∈ `mdg|obb|latmon|altsvc|cfgs|audl` (package names).
- `kind` is source-specific: `quote`, `book-event`, `latency-breach`, `alert`,
  `config-change`, `audit-event`.
- First event on connect is `data: {"src":"dashboard","kind":"hello","ts":...,
  "data":{"subscribed":[...sources that were reachable]}}`.
- Every **5 s** a `data: {"src":"dashboard","kind":"keepalive","ts":...}` line
  so proxies/clients see progress; a source that stops polling is announced with
  `{"src":...,"kind":"degraded","data":{"reason":"..."}}` once, and `recovered`
  on return.

### 3.3 Source polling (one daemon thread per source, inside the SSE request)

| Source | Poll | Emission rule |
|--------|------|---------------|
| mdg :7610 | `GET /quotes/{sym}?limit=1` every 2 s for each `?symbol=` (default: first 3 from `GET /symbols`) | emit each quote not already seen (track last `seq` per symbol) |
| obb :7620 | `GET /events?limit=100` every 2 s | emit events with `seq` greater than last seen |
| latmon :7670 | `GET /latency` every 2 s | emit `latency-breach` only when a stage transitions into breach (prev ok → breach) or recovers |
| altsvc :7700 | `GET /alerts/dispatch?limit=100` every 2 s | emit each dispatch entry whose `alert_id` is new |
| cfgs :7710 | `GET /changes/watch?timeout_ms=4000` (long-poll) | emit one `config-change` per returned change, then re-poll with new `since` |
| audl :7730 | `GET /events?limit=100` every 2 s | emit events with `event_id` greater than last seen |

All polls use a 1.5 s read timeout; a failed poll marks the source `degraded`
(announced once), polling continues. The SSE connection ends on client disconnect
(the handler detects a broken pipe on write and joins + stops its pollers) or on
`?max_age_s=` (default 3600) elapsed. Query params: `?symbol=` (repeatable,
mdg only), `?sources=a,b` (comma subset, default all six).

### 3.4 Handler threading model

`ThreadingHTTPServer` already gives one thread per SSE client. The handler runs
the aggregator loop inline (blocking the request thread is intended — it IS the
stream). `self.close_connection = True` after the stream ends. Writes go through a
`threading.Lock`-guarded `send_line` because the poller threads append to a
bounded `queue.Queue(maxsize=1000)` and the handler thread is the sole writer.
Queue-full (slow client) → drop-oldest (queue.get_nowait then put) so a stalled
browser can never wedge the source threads.

### 3.5 M2 errors & edge rules

- Bad `?sources=` value → 400 UI-202 (before the stream opens).
- Auth: `/live/*` is a **local** dashboard endpoint — no gateway token, no S15.
- No source reachable at connect time → still 200 stream with a `hello` listing
  `subscribed: []` plus one `degraded` per source (the UI shows a greyed panel).
- M2 unit tests: aggregator with fake pollers (no sockets), SSE frame format,
  dedup logic per source, keepalive cadence with ManualClock-style injected time.
  Live test: `curl -N --max-time 6 http://127.0.0.1:7760/live/stream` must show
  `hello` + at least one real event while the platform runs in `--mock` mode.

## 4. M3 — Dark-theme UI

### 4.1 Delivery

`GET /` serves `src/dash/static/index.html` (embedded via `Path(__file__).parent / "static"`)
with `Content-Type: text/html; charset=utf-8`; `GET /static/<file>` serves the
colleague files (`dashboard.css`, `dashboard.js`) with correct types and `no-cache`.
Only the two CSS/JS files + index.html ship — no build step, no bundler, no fonts
fetch (system monospace/sans stacks). Unknown `/static/*` → 404 UI-404 JSON.

### 4.2 Panels (single page, no router)

1. **Health grid** (top bar): 15 tiles, one per service port, green/amber/red
   from `GET /api/health` (polled every 5 s). Amber = live but `not_ready`.
2. **Portfolio** (tab): PnL table (`/api/portfolio/pnl`), per-symbol drilldown
   (`/api/portfolio/pnl/{symbol}` on row click), metrics cards (Sharpe/Sortino/
   maxDD from `/api/portfolio/metrics`), VaR bar, attribution split, sparkline
   from `/api/history` (native `<canvas>`, no chart lib).
3. **Settlement** (tab): run list (`/api/settlement/runs/{date}`), report viewer
   (`/api/settlement/reports/{date}`), discrepancies table
   (`/api/settlement/discrepancies`); in **not** read-only mode a settle form
   posting JSON to `/api/settlement/settle` and a finalize button.
   In read-only mode (`GET /api/health` says so, or any write 403s) forms are
   hidden/disabled with a `READ-ONLY` badge.
4. **Live** (tab): the M2 SSE panel — event feed (auto-scroll, color per
   `src`), per-source status chips from `hello`/`degraded`/`recovered`.
5. **Alerts & audit** (tab): recent alerts + audit events via the M2 stream
   (client-side filter on `kind`), no extra REST routes needed.
6. **Gateway stats** (footer strip): `GET /api/gateway-stats` polled every 10 s.

### 4.3 UI engineering rules

- Vanilla JS only (ES2019): `fetch`, `EventSource`, `setInterval`. No jQuery,
  no framework, no external CDNs (must work offline in air-gapped prod).
- CSS: CSS variables for the dark theme (`--bg:#0b0e14`, `--panel:#11151f`,
  `--fg:#c9d1d9`, accent green/red/amber), `prefers-reduced-motion` respected,
  tables sticky-header, number columns right-aligned monospace.
- Every REST call checks the §1.2 envelope: `resp.error` present → toast with
  `code` + `message`; the UI never shows raw HTML error text.
- XSS: all dynamic text inserted via `textContent`/`document.createElement` only,
  never `innerHTML` with server data.

### 4.4 M3 verification

- `GET /` returns 200 HTML containing the six panel ids;
  `GET /static/dashboard.js` / `.css` return 200 with correct Content-Type.
- Live: with the platform in `--mock`, the page loads (curl the three assets),
  health grid data present in `/api/health`, and the SSE stream is consumed
  (proven in M2's live check). `--read-only` boot hides settlement write forms
  (assert via the `read_only` flag in `/api/health` response).

## 5. Definition of done (whole tool)

M1+M2+M3 built, `python -m pytest tools/dashboard/tests -q` green, live
verification per §1.8/§3.5/§4.4 recorded in `.state/project-state.md`, STATE.md
Tools table row = `✅ M1+M2+M3 COMPLETE & VERIFIED`.
