# tools/dashboard

Operations dashboard for the HFT platform. **Tool, not a service**: it never
appears in the 15-service table or `dependency-map.json`. Full spec:
[`PLAN.md`](PLAN.md). Stdlib-only Python, `ThreadingHTTPServer`, port **7760**.

## Run

```sh
cd tools/dashboard
PYTHONPATH=src python3 -m dash.main \
    --bind 127.0.0.1 --port 7760 \
    --auth-url http://127.0.0.1:7720 \
    --gateway-url http://127.0.0.1:7750
```

`--read-only` refuses write-scope routes (`/api/settlement/settle|ingest|
finalize/{date}`) with `403 UI-205` before any upstream call.

## Endpoints (M1 — API proxy, health sweep, gateway stats)

| Route | Behaviour |
|-------|-----------|
| `GET /healthz`, `GET /readyz` | local liveness/readiness |
| `GET /api/portfolio/{pnl,pnl/{symbol},metrics,var,attribution,history}` | proxy → S15 → S9 :7690 (query forwarded verbatim) |
| `POST /api/settlement/settle`, `POST /api/settlement/ingest`, `POST /api/settlement/finalize/{date}` | proxy → S15 → S14 :7740 (write scope; body forwarded) |
| `GET /api/settlement/{reports/{date},runs/{date},discrepancies}` | proxy → S15 → S14 :7740 |
| `GET /api/health` | parallel sweep of all 15 ports 7610–7750 (healthz+readyz each) |
| `GET /api/gateway-stats` | verbatim S15 `/stats` + `dashboard` wrapper (token counters, read_only) |

Upstream responses pass through **verbatim** (status + body untouched); the
dashboard's own failures use `UI-NNN` codes in the standard §1.2 envelope.
Token handling: `POST /token` to S12 with `sub=dashboard`,
`scopes=[read,write]`, 1 h TTL; proactive refresh at 75 % TTL used, reactive
re-fetch on any gateway 401 (exactly one retry, then verbatim pass-through).

## Live stream (M2 — SSE aggregator)

`GET /live/stream` (Server-Sent Events) fans the whole platform out over one
chunked stream. Six daemon pollers (S1 mdg, S2 obb, S7 latmon, S10 altsvc,
S11 cfgs, S13 audl; 1.5 s read timeout) feed a bounded 1000-slot, drop-oldest
queue. Every event is the `{"src","kind","ts","data"}` envelope; the stream
opens with a `hello` (all subscribed sources) and sends a keepalive every 5 s.
Per-source dedup: mdg/obb by seq, altsvc by `alert_id`, audl by hex `event_id`,
latmon by breach/recovery transitions, cfgs by long-poll `since` cursor
(read timeout 5 s > 4 s watch). Degraded/recovered state is announced once per
transition. Query filters (rejected pre-stream with `400 UI-202`):
`?sources=`, `?symbol=`, `?max_age_s=`.

## UI (M3 — dark-theme SPA)

`GET /` serves `src/dash/static/index.html` (vanilla ES2019, `textContent`-only
DOM — no `innerHTML`), with six panels: health grid, portfolio, settlement,
live, alerts & audit, gateway footer. `GET /static/*` serves the whitelist
`{index.html, dashboard.css, dashboard.js}` with `no-cache` and explicit content
types; path traversal, dot-segments, and symlink escape all fall through to a
UI-404 JSON (nothing disclosed). Only the three files ship — no build step.
The UI polls `/api/health` every 5 s, `/api/gateway-stats` every 10 s, and
subscribes to `/live/stream` (EventSource) with client-side alert/audit
filtering plus a canvas sparkline. The settle/finalize forms are disabled and a
READ-ONLY badge is rendered whenever `/api/health` reports `read_only` (set by
`--read-only`).

## Test

```sh
cd tools/dashboard && PYTHONPATH=src python3 -m pytest tests/ -q
```

All three milestones are complete and verified: 94/94 pytest
(54 M1 + 27 M2 + 13 M3), live-booted against all 15 services (S1 `--mock`).
