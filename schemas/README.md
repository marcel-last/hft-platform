# `schemas/` — HFT Platform Wire & API Schemas

JSON Schemas (draft 2020-12) for the platform's canonical wire formats, the
dependency-map document, and the per-service HTTP request/response surfaces.
These are documentation + tooling artifacts: the services themselves are
stdlib-only (Python) / `std`-only (Rust) and do **not** parse or validate
against these files at runtime. The schemas are the single reference for what
each service sends and receives; where they disagree with the code, the code
wins and the schema must be fixed (see the "Known deviations" note in the
dependency-map `notes[]`).

## Layout

```
schemas/
├── README.md                          # this file
├── dependency-map.schema.json         # shape of the repo-root dependency-map.json
├── quote-wire.schema.json             # S1 canonical quote (CONVENTIONS §4)
├── trade-wire.schema.json             # S1 canonical trade print (CONVENTIONS §4)
├── book-snapshot-wire.schema.json     # S2 L2 book snapshot + top-of-book (CONVENTIONS §4)
├── common/
│   └── common.schema.json             # /healthz, /readyz, and the §1.2 error envelope
└── services/
    ├── s01-market-data-gateway.schema.json   # POST /subscribe
    ├── s02-order-book-builder.schema.json    # POST /subscribe-events, POST /rebuild
    ├── s03-strategy-engine.schema.json       # POST /strategies, POST /pause, POST /resume
    ├── s04-execution-gateway.schema.json     # POST /orders (order intent), PATCH /orders/{id}
    ├── s05-risk-manager.schema.json          # POST /pre-trade-check, PUT /limits, kill-switch
    ├── s06-position-keeper.schema.json       # POST /adjust, POST /corporate-action
    ├── s07-latency-monitor.schema.json       # POST /stamp
    ├── s08-data-quality-monitor.schema.json  # (read-only API; response views)
    ├── s09-portfolio-analytics.schema.json   # (read-only API; response views)
    ├── s10-alerting-service.schema.json      # POST /alerts, POST /alerts/ack
    ├── s11-config-service.schema.json        # PUT /config/{service}, POST /overrides, PUT /flags/{name}
    ├── s12-auth-service.schema.json          # POST /token, POST /verify, POST /revoke, /keys
    ├── s13-audit-logger.schema.json          # POST /events, GET /events/{id}, verify-chain
    ├── s14-settlement-service.schema.json    # POST /settle (fills + statement lines)
    └── s15-api-gateway.schema.json           # bearer auth requirement + proxy surface
```

## Conventions encoded in these schemas

- **Timestamps** are int64 nanoseconds since Unix epoch (`*_ns`, plus the wire
  shorthand `vt`/`rt`), per CONVENTIONS §2. No floats in latency math.
- **Every error response** (and every in-band error) uses the §1.2 envelope
  defined in `common/common.schema.json#/$defs/errorEnvelope`.
- **`/healthz`** → `{"status":"ok","service":...,"version":...}`;
  **`/readyz`** → `{"status":"ready"|"not_ready","reasons":[...]}` — see
  `common/common.schema.json#/$defs/healthz` and `#/$defs/readyz`.
- Wire dicts for market data (quote/trade/book) use the short key names of
  CONVENTIONS §4 (`px`, `qty`, `ven`, `act`, ...). HTTP API request/response
  bodies outside the hot wire path use descriptive snake_case names.
- Request bodies are validated strictly (`additionalProperties: false`) where
  the service enforces field guards; response schemas are
  `additionalProperties: true` because services freely add counters/fields to
  `/stats` and view endpoints.

## Known deviations from CONVENTIONS §4 (schemas match the code)

1. `v` (wire version) is an **integer** `1` on the quote/trade wire (not a
   string) — `mdg.models.quote_to_wire`.
2. The quote wire has no separate `v`-str / venue-native symbol pair: `sym`
   is the **canonical** symbol (venue-native mapping lives in gateway
   config); there is no separate venue-native key on the wire.
3. Trade prints carry the aggressor side in **`aggr`** (not `side`), and
   additionally `seq`, `xid` (venue execution id), `tick`, `q`, `nl` beyond
   the §4 table.
4. The quality tag `q` has **five** values: `FRESH`, `STALE_WARN`, `STALE`,
   `GAP_SUSPECT`, `OUT_OF_ORDER` (the §4 table lists three).
5. `nl` on the quote/trade wire is **normalization latency (rt − vt) in ns**,
   an integer — not a "net-change" boolean.

These are intentional implementation choices recorded in STATE.md; the
schemas document the wire as it actually is.

## How to validate

Any JSON-Schema 2020-12 validator works, e.g.
`python3 -m json.tool` won't validate (it only parses); a stdlib-free quick
check is available at repo level via `python3 -c` with `json.load` for
syntactic validity. For real validation, `jsonschema` (pip) or
`ajv` (npm) both support draft 2020-12.
