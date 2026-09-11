# HFT Platform — Binding Conventions

> These rules apply to **all 15 services** without exception. Any new service
> added to this monorepo MUST follow every rule below. Deviations require an
> explicit entry in `STATE.md` explaining the rationale.
>
> Section 13 (Agent Runtime Constraints) describes hard limits of the coding
> environment. It overrides any conflicting habit inherited from earlier services.

---

## 1. Error Handling

### 1.1 Per-Service Code Prefixes

| Service | Prefix | Example |
|---------|--------|---------|
| market-data-gateway | `MDG-` | `MDG-001` |
| order-book-builder | `OBB-` | `OBB-003` |
| strategy-engine | `STE-` | `STE-012` |
| execution-gateway | `EXG-` | `EXG-007` |
| risk-manager | `RKM-` | `RKM-001` |
| position-keeper | `POS-` | `POS-004` |
| latency-monitor | `LAT-` | `LAT-002` |
| data-quality-monitor | `DQM-` | `DQM-005` |
| portfolio-analytics | `PFA-` | `PFA-001` |
| alerting-service | `ALT-` | `ALT-003` |
| config-service | `CFG-` | `CFG-001` |
| auth-service | `AUT-` | `AUT-002` |
| audit-logger | `AUD-` | `AUD-001` |
| settlement-service | `STL-` | `STL-004` |
| api-gateway | `API-` | `API-001` |

Codes are `PREFIX-NNN`, three digits, zero-padded. Services group codes by class
(e.g. `AUT-0xx` config/boot, `AUT-2xx` request/validation) — this is a convention
of ranges, not a change to the digit count; `AUT-207` is still valid `PREFIX-NNN`.

### 1.2 Standard Error Envelope

Every error response (HTTP or in-band) MUST use this JSON shape:

```json
{
  "error": {
    "code": "MDG-001",
    "message": "Human-readable description of what went wrong.",
    "service": "market-data-gateway",
    "retryable": true,
    "context": {
      "venue_id": "EUREX",
      "symbol": "FESX",
      "detail": "additional structured data useful for debugging"
    }
  }
}
```

Rules:
- `code` is always `PREFIX-NNN` (3 digits, zero-padded).
- `message` is a single sentence, no newlines.
- `service` is the directory name (e.g. `"market-data-gateway"`).
- `retryable` is a boolean: `true` if the caller should retry with backoff.
- `context` is an object (may be empty `{}`) with service-specific debug fields.

### 1.3 Python Exception Hierarchy

Each Python service defines its exception tree in `errors.py`:

```python
class {PREFIX}Error(Exception):
    """Base exception for the {service} service."""
    code: str = "{PREFIX}-000"
    retryable: bool = False

class SpecificError({PREFIX}Error):
    code = "{PREFIX}-001"
    retryable = True
```

A module-level helper serializes any exception to the envelope:

```python
def error_envelope(exc: Exception) -> dict:
    ...
```

### 1.4 Rust Error Handling (Rust services S4, S7, S10, S12, S15)

- Use a per-service `Error` enum implementing `std::fmt::Display` + `std::error::Error`.
- Each variant carries the same `(code, message, retryable)` triple.
- HTTP error responses serialize to the identical JSON envelope as Python.
- No panics on the request path; use `Result<T, Error>` throughout.

---

## 2. Timestamps & Time

- **All timestamps on hot paths are `int64` nanoseconds since Unix epoch.**
- Helper: `now_ns() -> int` (Python) / `SystemTime::now().duration_since(UNIX_EPOCH).as_nanos()` (Rust).
- **No floating-point arithmetic in latency math.** Convert to µs or ms only at the serialization/display boundary.
- Configuration values expressed in milliseconds use `_ms` suffix (e.g. `stale_book_ttl_ms`).
- Wire format uses field names `vt` (venue timestamp ns) and `rt` (receive timestamp ns).

---

## 3. HTTP Layer (Python Services)

- **Stdlib only.** No Flask, FastAPI, aiohttp, or any third-party HTTP library.
- Server: `http.server.ThreadingHTTPServer` with a custom `BaseHTTPRequestHandler`.
- Routing: a minimal regex-based `Router` class supporting `{param}` path patterns:
  ```python
  router.add("GET", r"/books/(?P<symbol>[A-Z0-9_&]+)", handler_fn)
  ```
- All handlers return `(status_code: int, body: dict)` tuples.
- JSON serialization via `json.dumps` / `json.loads`.
- Content-Type is always `application/json`.
- Health endpoints (every service):
  - `GET /healthz` → `{"status": "ok", "service": "<name>", "version": "<ver>"}`
  - `GET /readyz` → `{"status": "ready"|"not_ready", "reasons": [...]}`

---

## 4. Wire Format (Market Data)

The canonical quote wire dict used between S1 (gateway) and all downstream consumers:

| Key | Type | Description |
|-----|------|-------------|
| `v` | str | Canonical symbol (e.g. `"EU_STOXX50_CONT"`) |
| `sym` | str | Venue-native symbol (e.g. `"FESX"`) |
| `ven` | str | Venue ID (e.g. `"EUREX"`) |
| `seq` | int | Sequence number from venue |
| `act` | str | Action: `N`=new, `M`=modify, `D`=delete, `E`=execute, `B`=begin, `O`=other |
| `side` | str | `BID`, `ASK`, `LAST`, or `UNKNOWN` |
| `px` | float | Price |
| `qty` | int | Quantity (lots) |
| `lvl` | int | Depth level (1 = top) |
| `tick` | float | Tick size for the instrument |
| `q` | str | Data quality tag: `FRESH`, `STALE_WARN`, `STALE` |
| `vt` | int | Venue timestamp (ns since epoch) |
| `rt` | int | Receive timestamp at gateway (ns since epoch) |
| `nl` | bool | `true` if this is a net-change message (delta vs. absolute) |

### Trade Print Wire Format

| Key | Type | Description |
|-----|------|-------------|
| `sym` | str | Canonical symbol |
| `ven` | str | Venue ID |
| `px` | float | Execution price |
| `qty` | int | Executed quantity |
| `side` | str | Aggressor side: `BID` or `ASK` |
| `vt` | int | Venue timestamp (ns) |
| `rt` | int | Receive timestamp (ns) |

### Book Snapshot Wire Format (S2 → downstream)

```json
{
  "sym": "EU_STOXX50_CONT",
  "ven": "EUREX",
  "health": "HEALTHY",
  "tick": 1.0,
  "bids": [[5001.0, 12], [5000.0, 8]],
  "asks": [[5002.0, 5], [5003.0, 20]],
  "tob": {
    "bb_px": 5001.0, "bb_qty": 12,
    "ba_px": 5002.0, "ba_qty": 5,
    "mid": 5001.5, "spread": 1.0, "spread_ticks": 1,
    "imb": 2.4, "ts": 1788940747832091143
  },
  "msgs": 42,
  "rebuilds": 1
}
```

---

## 5. Configuration Pattern

Every Python service follows this exact pattern in `config.py`:

```python
from dataclasses import dataclass, field
from typing import ...

@dataclass(frozen=True)
class SubConfigA:
    """Docstring explaining this namespace."""
    field1: int = 42
    field2: str = "value"

@dataclass(frozen=True)
class ServiceConfig:
    name: str = "<service-dir-name>"
    version: str = "1.0.0"
    env: str = "production"
    sub_a: SubConfigA = field(default_factory=SubConfigA)
    # ... other sub-configs

def validate_config(cfg: ServiceConfig) -> List[str]:
    """Return list of error strings; empty list = valid."""
    errors: List[str] = []
    # ... checks
    return errors

# Module-level singleton used by all other modules.
CONFIG = ServiceConfig()
```

Rules:
- All dataclasses are `frozen=True` (immutable after construction).
- No environment variable reading in config.py (that's the config-service's job at deploy time).
- `validate_config` is called once at boot in `main.py`; if it returns errors, the service logs them and exits(1).

Rust services follow the same spirit: an immutable config struct (or set of structs)
with a `validate(&self) -> Vec<String>` method, checked once in `main.rs` before
the server starts.

---

## 6. Module Layout (Python Services)

```
services/<service-dir>/
├── pyproject.toml
├── src/
│   └── <pkg>/
│       ├── __init__.py      # re-exports public API
│       ├── config.py        # frozen dataclasses + validate_config + CONFIG
│       ├── models.py        # domain dataclasses, enums, wire serializers
│       ├── errors.py        # exception hierarchy + error_envelope()
│       ├── <core>.py        # main business logic (e.g. book_engine.py)
│       ├── controller.py    # request handlers (receives parsed params)
│       ├── router.py        # Router class + build_router() factory
│       └── main.py          # entrypoint: parse args, validate config, start server
└── tests/
    └── test_<core>.py      # unit tests (stdlib unittest or pytest-compatible)
```

---

## 7. Module Layout (Rust Services)

```
services/<service-dir>/
├── Cargo.toml               # lib + bin targets, zero dependencies
├── .gitignore               # /target
├── src/
│   ├── lib.rs               # module root
│   ├── main.rs              # entrypoint: validate config, serve, block
│   ├── json.rs              # hand-rolled JSON codec — COPIED from templates/rust/ (§7.1)
│   ├── router.rs            # method+path router with {param} segments — COPIED from templates/rust/
│   ├── server.rs            # TcpListener accept loop, request parsing, response writer — COPIED
│   ├── config.rs            # config structs + validate()
│   ├── models.rs            # domain types, wire (de)serialization
│   ├── errors.rs            # Error type + Display impl + envelope serializer
│   ├── core.rs              # main business logic (+ Clock trait: System/Manual, where time matters)
│   ├── http.rs              # request handlers ONLY (service-specific); the loop lives in server.rs
│   └── crypto.rs            # only when required (S12, S15) — COPIED from templates/rust/
└── tests/
    ├── common/
    │   └── mod.rs           # shared fixtures: manager/clock builders, server::request wrapper
    ├── <area_a>.rs          # one file per functional area, each ≤ 250 lines
    ├── <area_b>.rs
    └── http.rs              # HTTP end-to-end tests
```

Rust services use **only `std`** (no external crates) to match the zero-dependency
constraint. HTTP is implemented with `std::net::TcpListener` + manual request parsing
(all inside the copied `server.rs`).

S4, S7 and S10 predate this layout (single `tests/core_test.rs`, server loop inside
`http.rs`). They are complete and verified; do **not** retrofit them. New Rust services
(S12, S15) use the layout above.

### 7.1 Boilerplate Templates (copy, never rewrite, never read in full)

`templates/rust/` holds verified, service-agnostic files that are byte-identical across
Rust services. They were compiled together with `-D warnings` and ship their own
`#[cfg(test)]` known-answer tests, which run automatically under `cargo test` in every
service that copies them:

| File | Contents |
|------|----------|
| `json.rs` | `Value` enum, parser, serializer, `From`/accessor helpers (with tests) |
| `router.rs` | method+path router with `{param}` segments, query/percent-decode (with tests) |
| `server.rs` | accept loop, request parsing, body extraction, response writer, error envelope, `request()` client (with tests) |
| `crypto.rs` | SHA-256, HMAC-SHA256, base64url, hex, constant-time compare, `/dev/urandom` (with FIPS/RFC KATs) |
| `API.md` | public signatures of the four files above + an `http.rs` wiring sketch (~110 lines) |

Rules:
- Scaffold a Rust service by copying the templates into `src/`. The agent's shell is
  `sh`, so use a loop (brace expansion `{a,b}` is a bashism that fails here):
  ```sh
  for f in json router server; do cp templates/rust/$f.rs services/<svc>/src/; done
  # add crypto to the list only when the service needs hashing/tokens (S12, S15):
  for f in json router server crypto; do cp templates/rust/$f.rs services/<svc>/src/; done
  ```
  Run this from the repo root, so the relative `templates/rust/` path resolves.
- **Never rewrite these modules from memory.** Regenerating them costs tokens and
  reintroduces bugs that were already fixed (the hand-rolled SHA-256 is the known trap).
- **Never read them in full.** Read `templates/rust/API.md` for their interfaces.
- **Do not read a previous service's source to "match style."** This document and the
  templates define the style.
- **Do not re-test what the templates already test.** `json.rs` and `crypto.rs` carry
  their own KATs; a service's own test files must not re-verify SHA-256/HMAC/base64url/
  JSON round-trips.
- A bug found in a template is fixed in `templates/rust/` first, then re-copied into the
  service; note the fix in `STATE.md`.
- Fallback only if `templates/rust/` does not exist: copy `json.rs` and `router.rs`
  from the most recently completed Rust service in `STATE.md`, record the deviation in
  `STATE.md`, and do not rewrite them.

---

## 8. Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Python package | lowercase, no hyphens | `mdg`, `obb`, `ste` |
| Service directory | kebab-case | `market-data-gateway` |
| Config fields | snake_case | `stale_book_ttl_ms` |
| Wire keys | short abbreviations (see §4) | `px`, `qty`, `vt` |
| Error codes | `PREFIX-NNN` | `OBB-003` |
| HTTP endpoints | kebab-case, plural nouns | `/order-books`, `/book-events` |
| Rust modules | snake_case files | `book_engine.rs` |
| Rust types | PascalCase | `OrderBook`, `BookEvent` |

---

## 9. Logging

- Python: `logging` module, logger name = package name (e.g. `"obb.book_engine"`).
- Format: `%(asctime)s %(name)s %(levelname)s %(message)s`.
- Hot-path code uses `logger.debug`; user-facing events use `logger.info`; anomalies use `logger.warning`; unrecoverable errors use `logger.error`.
- No `print()` in library code (only in `main.py` for boot messages).

---

## 10. Testing

- Python: pytest-compatible test files (plain functions with `assert`).
- Each service has at least one test file exercising the core logic.
- Tests must be runnable with: `cd services/<dir> && python -m pytest tests/ -v`
- No network calls in unit tests; use in-memory fakes or mock transports.
- Rust: integration tests live under `tests/`, split by functional area as shown in §7,
  with shared fixtures in `tests/common/mod.rs` (imported with `mod common;` in each
  test file). Each test file is ≤ 250 lines. Small `#[cfg(test)] mod tests` blocks
  inside a module are fine for unit-level checks. Runnable via `cargo test`.
- **Do not duplicate template tests** (§7.1): `json.rs`/`crypto.rs` self-verify, so a
  service's own tests cover only service logic (issuance, verification, revocation,
  rotation, HTTP surface), never the primitives.
- Time-dependent behaviour (expiry, cooldowns, escalation) is tested through a
  `ManualClock`, never by sleeping.

---

## 11. Inter-Service Communication

- **Synchronous (request/response):** HTTP POST/GET with JSON bodies.
- **Asynchronous (streaming):** HTTP GET returning a chunked/streamed JSON-lines response (one JSON object per line), or long-poll.
- **Service discovery:** Hardcoded host:port in config for now; the config-service (S11) will later provide dynamic discovery.
- **Timeouts:** All outbound HTTP calls use explicit timeouts (connect: 500ms, read: configurable per call). Rust services use `server::request(...)` from the template for outbound calls.
- **Retries:** Exponential backoff with jitter, max 3 attempts, only for `retryable: true` errors.

---

## 12. Versioning

- All services start at version `1.0.0`.
- The version string appears in: `config.py` (Python) / `Cargo.toml` (Rust), the `/healthz` response, and the `pyproject.toml` / `Cargo.toml` metadata.

---

## 13. Agent Runtime Constraints

These are hard limits of the coding environment (output-token cap and context window),
not style preferences. They apply to every session and every service. When a habit
inherited from an earlier service conflicts with this section, this section wins.

### 13.1 File Writes

- A single `write_file` or `replace_file_content` call contains **≤ 250 lines** of content.
- Files longer than 250 lines are written in parts: create the file with part 1, then
  append each further part with an edit anchored on the **last line** of the previous
  part. Announce "Part N of M" before each call.
- Anchor edits on a single unique line, never on a multi-line block; multi-line anchors
  fail on whitespace mismatches and waste a round-trip.
- Never begin a write that cannot finish in the current response. If unsure, use fewer
  lines per part.
- Prefer several small files over one large file (see §7 test layout).
- Inside tool-call arguments: newlines are `\n`, double quotes are `\"`, backslashes
  are `\\`. No raw line breaks, no commentary inside arguments.

### 13.2 Reads

- At session start read **only** `STATE.md` and `CONVENTIONS.md`. Read
  `.state/project-state.md` and `dependency-map.json` only when a specific question
  needs them.
- Never read a whole previous service. Never read a template file in full; read
  `templates/rust/API.md`.
- Use `read_file` with a line range. Never re-read a file written in the current session.
- Directory listings: at most two levels deep, and only for the service being built.

### 13.3 Tool Output

The compiler is authoritative, no stale cache.

Never two builds with no edit between"

Compiler and test output is the most expensive thing that enters the context. Always
bound it:

```
cargo build --message-format=short 2>&1 | head -60     # run ONCE; never a second verbose run
cargo test -q 2>&1 | tail -60
python -m pytest tests/ -q 2>&1 | tail -40
```

- Never `cat` a file. Never dump a directory tree of the whole repo.
- When debugging a failing test, add a targeted assertion or a single `eprintln!`, not
  round-by-round instrumentation dumps.

### 13.4 Task Checklist

- One task per source module and one task per test file.
- "Write tests" or "write the test suite" is never a single task.

### 13.5 Session Checkpoints

Every service passes through three milestones:

1. **Source compiles clean** — zero errors, zero warnings.
2. **Tests pass** — `cargo test` / `pytest` fully green.
3. **Live verification** — service boots on its port; `/healthz`, `/readyz` and every
   endpoint exercised with correct envelopes.

Rules:
- Update `STATE.md` (status table, next steps, changelog) at **each** milestone, not
  only at completion.
- **After milestone 1, stop and report.** Tests are written in a fresh session, which
  begins by re-reading `STATE.md` and `CONVENTIONS.md`.
- After milestone 3, also update `.state/project-state.md` and `dependency-map.json`.
- If a session ends unexpectedly, the next session first verifies on disk what
  `STATE.md` claims exists (`ls services/<svc>/src services/<svc>/tests`) before
  writing anything, and corrects `STATE.md` if they disagree.