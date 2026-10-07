# RegisterWatch API: design review and migration plan

Status: proposed · Written 2026-10-07 against `main` @ `1e14668` · Target: API `v1` (package 0.3.0)

This file is both the design review and the work order. An agent works through it
top to bottom. **A phase is done only when every test in that phase passes**, and
the phase's checklist is struck out item by item as evidence is recorded.

---

## 0. How to use this plan (rules for the executing agent)

1. Work phases in order. A later phase may depend on an earlier one; the
   dependency is stated at the top of each phase.
2. Each step has an ID (`P3.2`) and a set of tests (`T3.2.a`, `T3.2.b`, ...).
   A test is a command plus the exact pass condition. "Looks fine" is not a pass.
3. When a test passes, record it in `docs/plan-evidence.md` as one line:
   `T3.2.a | 2026-10-08 | <git sha> | <command> | <one-line result>`.
4. Then strike the item in the phase checklist by changing
   `- [ ] T3.2.a ...` to `- [x] ~~T3.2.a ...~~`.
   Never strike an item without an evidence line. Never strike a test that was
   skipped; mark it `- [ ] T3.2.a ... (BLOCKED: reason)` instead.
5. A phase's last checklist line is its gate (`GATE P3`). Strike it only when
   every other line in the phase is struck, and the full suite
   (`uv run pytest -q` plus `uv run lint-imports`) is green on the same commit.
6. Tests marked **[pg]** need Postgres:
   `docker run -d --rm --name rw-pg -e POSTGRES_PASSWORD=rw -e POSTGRES_DB=rw -p 55432:5432 postgres:16-alpine`
   and `REGISTERWATCH_TEST_DATABASE_URL=postgresql://postgres:rw@localhost:55432/rw`.
   Tests marked **[box]** need the built Docker image running locally
   (`docker build -t registerwatch:plan . && docker run ...`, given in Phase 9).
   Tests marked **[edge]** need the deployed service behind its real TLS proxy;
   run them against the staging URL in `$RW_BASE` (never production for load tests).
7. Do not change behaviour outside a step. If a step turns out wrong, edit this
   plan in the same commit and say why in the evidence log.

---

## 1. Validation of the current design

### 1.1 What was checked

| Check | How | Result |
|---|---|---|
| Unit tests | `uv run pytest -q` | 128 passed, 9 skipped (Postgres tests skipped) |
| Generated spec | `app.openapi()` dumped to `openapi.json` | OpenAPI 3.1.0, 12 operations, 2 component schemas |
| Spec lint | `npx @redocly/cli lint openapi.json` (recommended ruleset) | **13 errors, 4 warnings**: `security-defined` ×12, `no-empty-servers` ×1, `operation-4xx-response` ×3, `info-license` ×1 |
| Code read | `src/registerwatch/api.py`, `query.py`, `db/engine.py`, `db/schema.py`, `config.py`, `Dockerfile`, Supabase migrations | findings below |

### 1.2 What is already right (keep it)

- Read and write are separated, ingest is never open (a missing `INGEST_TOKEN` is a 503, not an open trigger), tokens are compared with `secrets.compare_digest`.
- Every SQL identifier comes from a register declaration and goes through `psycopg.sql.Identifier`; every value is bound. Unknown filter columns are refused (400), not passed through.
- `snake_case` is used consistently in paths, parameters and bodies.
- Ingest answers 202 and runs in the background, which is the right shape for a pg_net caller with a 30 s timeout.
- `/health` touches nothing; `/status` is an honest staleness alarm.
- The storage model (`first_seen_snapshot_id` / `removed_snapshot_id`) already supports a proper change feed and snapshot-anchored caching. The API just does not expose it yet.

### 1.3 Findings

Severity: **H** breaks clients or correctness, **M** violates the contract or REST semantics, **L** hygiene.

| # | Sev | Finding | Evidence |
|---|---|---|---|
| F1 | H | **Unstable pagination.** Rows are ordered `ORDER BY 1, 2` on register columns, which are not unique; with `OFFSET`, rows repeat or vanish across pages, and every daily ingest shifts offsets. The `current_*` views do not expose `id` or `row_hash`, so no stable key is available to clients. | `query.py` `rows()`; `db/schema.py` view definition |
| F2 | H | **Unbounded and unvalidated paging inputs.** `offset` accepts negatives (Postgres raises, the client gets a bare 500). `limit` on `/changes` and `/search` has no maximum; `/changes?limit=10000000` runs per table per register, and `/changes?limit=-1` is a 500 (found by schemathesis in Phase 1). `limit=-5` on rows silently becomes 0. | `api.py` `get_changes`, `search`; `query.py` `MAX_LIMIT` only in `rows()` |
| F3 | H | **Process-local ingest state.** The one-at-a-time guard is a `threading.Lock` and the last result lives in `_last` (a dict in memory). Two replicas, or a restart, mean two concurrent batches and a lost result; `/ingest/last` 404s after every deploy. | `api.py` `_running`, `_last` |
| F4 | H | **`/changes` silently truncates.** Per table it returns at most `limit` added and `limit` removed rows with no indication more exist, so a mass change looks like a small one. | `query.py` `changes()` |
| F5 | M | **No security schemes in the spec.** Auth is a plain `authorization` header parameter on each operation; generated clients get a string argument instead of auth. Redocly: `security-defined` ×12. 401s carry no `WWW-Authenticate`. A valid read token on an ingest route gets 401 where 403 is correct. | lint output; `require_token`, `require_read` |
| F6 | M | **Untyped responses.** Every 200 is `{"type":"object","additionalProperties":true}`; no examples. Clients cannot be generated usefully and nothing tests the shape. | `openapi.json` |
| F7 | M | **Error format is not RFC 9457.** Errors are FastAPI's `{"detail": "..."}` (or a list for 422) as `application/json`; no stable `type` URIs; the 401/403/404/409/429/503 cases are undocumented in the spec (only 422 is listed). | `api.py`; lint `operation-4xx-response` |
| F8 | M | **Verbs and RPC shapes in URIs.** `/check/domain/{domain}` (verb), `POST /ingest/{slug\|all}` (a magic `all` path value creating an unnamed thing), `POST /jurisdictions/{code}/ingest` (verb as sub-resource), `?wait=true` (holds an HTTP request open for minutes). | `api.py` |
| F9 | M | **No versioning.** No `/v1`, no deprecation policy, and a Supabase cron job hard-codes `/ingest/` in SQL, so any rename breaks the schedule. | `api.py`; `supabase/migrations/20261004000004_schedule_daily_ingest.sql` |
| F10 | M | **Filter namespace collides with reserved parameters.** Column filters are "any other query parameter", so a future column named `q`, `limit`, `offset`, `cursor` or `sort` becomes unfilterable, and a typo in `limit` becomes a 400 "unknown column". No register has such a column today (checked), but the next one may. | `api.py` `get_rows` |
| F11 | M | **Redundant, ambiguous row path.** `/jurisdictions/{code}/{slug}/{table}`: the slug is globally unique, so `code` adds a way to be wrong (`/jurisdictions/gb/pl_mf/...` is a 404) and the 3-segment wildcard sits next to `/jurisdictions/{code}/changes` and `/ingest`, so a register named `changes` would be unroutable. | route table |
| F12 | M | **No rate limiting or caching.** `/search` runs two queries per table across ~30 tables with `ILIKE '%q%'` on every text column (sequential scans); `/check/domain` uses `LIKE '%.x'`. No 429, no `RateLimit` headers, no `ETag`/`Cache-Control` even though data changes at most a few times per day. | `query.py` |
| F13 | M | **`/status` overloads 503.** It is both the uptime alarm and the status page, so a stale register makes the status page itself an error and a load balancer readiness probe would pull a healthy instance. | `api.py` `status` |
| F14 | M | **Connection pool can starve.** Pool `max_size=4`, FastAPI sync handlers run in a 40-thread pool, no pool timeout handling, no `statement_timeout`. The fifth concurrent `/search` waits 30 s and then 500s. `?wait=true` holds one connection for minutes. | `db/engine.py` |
| F15 | L | Spec hygiene: auto-generated `operationId`s (`get_rows_jurisdictions__code___slug___table__get`), no `servers`, no `license`, `/registers` tagged `health`, no `contact`. | lint; `openapi.json` |
| F16 | L | Platform: the image runs as root, has no `HEALTHCHECK`, uvicorn runs without `--proxy-headers`, with default keep-alive (5 s, below most load balancer idle timeouts, which causes intermittent 502s), no graceful-shutdown timeout for a running ingest, no response compression for 1000-row pages, no CORS policy. | `Dockerfile`, `cli.py` `cmd_serve` |
| F17 | L | Timestamps: `since` accepts naive datetimes and assumes UTC silently; responses mix `datetime` objects serialised by FastAPI with ISO strings built by hand in `_last`. | `api.py` |
| F18 | H | **NUL bytes in text parameters are a 500.** `/search?q=` containing `%00` reaches Postgres, which raises `DataError: text fields cannot contain NUL`. Found by schemathesis in Phase 1. Same path for `filter` values and `q` on rows. | `query.py` `_like`, `rows()` |

Open question, not scheduled (needs a product decision): jurisdiction code `CA`
is labelled "Canada — Kahnawà:ke". Kahnawà:ke is not Canada's federal regulator,
so `CA` will mislead clients the day a federal or another provincial register is
added. A private-use code (for example `CA-KHW`) would be a breaking data change and
belongs in its own versioned change.

---

## 2. Target design (v1)

### 2.1 Resource model

```
Jurisdiction (code)  1───*  Register (slug)  1───*  Table (name)  1───*  Row (id)
                                  │                                       │
                                  1                                       │ first_seen / removed
                                  *                                       │
                             Snapshot (id)  ◄─────────────────────────────┘
                                  ▲
IngestRun (id)  *───*  Register   │ produces 1 snapshot per register
                                  │
DomainStatus (domain)  — computed view over Rows of tables with host/domain/hosts columns
Change (snapshot_id, row)  — a Row event: added in snapshot x, or removed in snapshot x
```

### 2.2 Endpoints

All under `/v1`. Health probes stay unversioned.

| Method | Path | Purpose | Auth | Replaces |
|---|---|---|---|---|
| GET | `/v1/jurisdictions` | list | read | `/jurisdictions` |
| GET | `/v1/jurisdictions/{code}` | one, with its registers (links) | read | `/jurisdictions/{code}` |
| GET | `/v1/jurisdictions/{code}/changes` | change feed across the jurisdiction's registers | read | `/jurisdictions/{code}/changes` |
| GET | `/v1/registers` | list, with tables and columns | read | `/registers` |
| GET | `/v1/registers/{slug}` | one, with freshness | read | — |
| GET | `/v1/registers/{slug}/tables/{table}` | table schema (columns, types) | read | — |
| GET | `/v1/registers/{slug}/tables/{table}/rows` | current rows; `filter[col]=`, `q`, `cursor`, `limit` | read | `/jurisdictions/{code}/{slug}/{table}` |
| GET | `/v1/registers/{slug}/tables/{table}/rows/{id}` | one row, with its history markers | read | — |
| GET | `/v1/registers/{slug}/changes` | change feed for one register; `since`, `until`, `cursor` | read | — |
| GET | `/v1/registers/{slug}/snapshots` | snapshot history incl. incomplete runs and reasons | read | — |
| GET | `/v1/search` | `q`, `jurisdiction[]`, `cursor` | read | `/search` |
| GET | `/v1/domains/{domain}` | where licensed, where blocked, matching rows | read | `/check/domain/{domain}` |
| POST | `/v1/ingest-runs` | start a run; body `{registers?: [...], jurisdiction?: "gb", force, accept_count_delta}`; `Idempotency-Key` header | ingest | `POST /ingest/{slug\|all}`, `POST /jurisdictions/{code}/ingest` |
| GET | `/v1/ingest-runs` | run history | ingest | `/ingest/last` |
| GET | `/v1/ingest-runs/{id}` | one run, per-register results | ingest | `/ingest/last` |
| GET | `/v1/status` | freshness per register; always 200; `?strict=true` returns 503 when stale (for uptime monitors) | open | `/status` |
| GET | `/livez` | process alive, touches nothing | open | `/health` |
| GET | `/readyz` | database reachable within 2 s, else 503 | open | — |

### 2.3 Conventions (apply everywhere)

- **Naming:** `snake_case` for fields and query parameters; plural nouns for collections; kebab-case for multi-word path segments (`ingest-runs`).
- **Collections:** `{"data": [...], "pagination": {"next_cursor": "...|null", "has_more": bool, "limit": n}}`, plus an RFC 8288 `Link: <...>; rel="next"` header. `total` only with `?include_total=true` (it costs a `count(*)`).
- **Pagination:** opaque cursor = base64url of `{"k": <last id>, "s": <snapshot id the page was read at>, "f": <hash of filters>}`. Keyset on the row's `id`. A cursor from a different filter set is a 400. `limit` default 100, `1 ≤ limit ≤ 1000`.
- **Change feed:** flat list of `{"change": "added"|"removed", "at", "snapshot_id", "register", "table", "row"}`, ordered by `(snapshot_id, id)`, cursor-paged. Never truncated without `has_more: true`.
- **Errors:** RFC 9457 `application/problem+json` with `type`, `title`, `status`, `detail`, `instance` (the request path), a `request_id` extension member, and `errors[]` for field-level problems. Catalog in 2.5.
- **Auth:** `securitySchemes.ReadToken` and `securitySchemes.IngestToken`, both HTTP bearer. 401 + `WWW-Authenticate: Bearer realm="registerwatch"` when missing or wrong; 403 when a valid read token calls an ingest route. Ingest token also reads (kept).
- **Caching:** `ETag` = hash of the newest complete snapshot id of every register the response depends on; `Cache-Control: public, max-age=300` when reads are open, `private` when `READ_TOKEN` is set; `If-None-Match` answers 304.
- **Rate limiting:** per token, or per client IP when open. Read: 600 req/min; `/v1/search` and `/v1/domains`: 60 req/min; ingest: 10 req/min. Headers `RateLimit-Policy` and `RateLimit` (IETF httpapi draft), 429 + `Retry-After`.
- **Time:** every timestamp RFC 3339 with offset, UTC. A naive `since` is a 400.
- **Request id:** accept `X-Request-Id` (or generate a UUIDv7), echo it, log it, and put it in `request_id` of problems.

### 2.4 Versioning and deprecation policy

- URI major version (`/v1`). Additive changes (new fields, endpoints, optional params) ship in place; clients must ignore unknown fields.
- A breaking change means `/v2`. `/v1` is then supported for at least 6 months, announced with `Deprecation` (RFC 9745) and `Sunset` (RFC 8594) headers and `Link: <...>; rel="successor-version"` on every response.
- The current unversioned routes become **legacy**: they keep working for one minor release (0.3.x), answer with `Deprecation: @<epoch of 0.3.0 release>`, `Sunset: <0.3.0 + 90 days>` and a `successor-version` link, and are removed in 0.4.0. The Supabase cron moves to `/v1/ingest-runs` in the same release that adds it.
- Breaking-change detection is automated (`oasdiff breaking`, Phase 1) against the committed `openapi/v1.yaml`.

### 2.5 Error catalog

Base: `https://github.com/mishmish004/RegisterWatch/blob/main/docs/problems.md#<slug>`: one section per type in
`docs/problems.md`, so each URI resolves to what the type means and what to do about it.
(First written as `https://registerwatch.dev/problems/`, served from `/v1/problems/{slug}`. Changed in
Phase 3: no one controls that domain, and a page in the public repository resolves without a route
the API has to serve, secure and rate-limit.)

| Status | `type` slug | When |
|---|---|---|
| 400 | `invalid-parameter` | bad `limit`, negative values, naive `since`, malformed domain; `errors[]` names each field |
| 400 | `unknown-filter-column` | `filter[x]` where the table has no column `x`; `detail` lists valid columns |
| 400 | `invalid-cursor` | cursor undecodable, or from a different filter set |
| 401 | `unauthenticated` | missing or wrong bearer token |
| 403 | `forbidden` | read token on an ingest route |
| 404 | `not-found` | no operation at this path |
| 404 | `jurisdiction-not-found`, `register-not-found`, `table-not-found`, `row-not-found`, `ingest-run-not-found` | unknown identifiers; `detail` lists valid ones where short |
| 405 | `method-not-allowed` | the path exists, the method does not; `Allow` lists the ones it takes |
| 409 | `ingest-in-progress` | a run is active; `Retry-After` and the active run's URL in `active_run` |
| 415 | `unsupported-media-type` | POST body not `application/json` |
| 422 | `idempotency-key-reused` | same key, different body |
| 429 | `rate-limited` | over the limit; `Retry-After` |
| 503 | `ingest-disabled` | `INGEST_TOKEN` not configured |
| 503 | `database-unavailable` | pool timeout or connection failure; `Retry-After: 5` |
| 504 | `query-timeout` | statement exceeded `statement_timeout` |
| 500 | `internal` | anything else; no stack trace, only `request_id` |

Phase 3 added `not-found` and `method-not-allowed` (v1 answers every error as a problem, so these
needed types too) and dropped `ambiguous-table`: legacy routes keep FastAPI's `{"detail": ...}`
bodies until they are removed, and no v1 route can be ambiguous.

---

## 3. Phases

Phases run in this order: **1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12**.
Phases 1–8 are application changes, 9 is OS/container, 10 is network, 11 is
the scheduler migration, 12 is legacy removal and sign-off.

New test files referenced below: `tests/test_contract.py`, `tests/test_errors.py`,
`tests/test_pagination.py`, `tests/test_ingest_runs.py`, `tests/test_caching.py`,
`tests/test_ratelimit.py`, `tests/test_legacy.py`, and shell scripts under
`scripts/verify/` (`os.sh`, `net.sh`, `load.py`). Every script exits non-zero on
failure and prints one `PASS`/`FAIL` line per check, so its output is the evidence.

---

### Phase 1. Contract baseline and tooling

Depends on: nothing. Purpose: make the spec a committed, linted, diffable artifact
so every later phase is measured against it.

**P1.1 Commit the spec and a lint config.**
Add `registerwatch openapi --write` (CLI) which writes `openapi/v1.yaml` from
`app.openapi()`. Add `redocly.yaml` extending `recommended` with
`operation-4xx-response: error`, `operation-operationId: error`,
`no-unused-components: error`, `info-license: error`, `security-defined: error`.
- T1.1.a `uv run registerwatch openapi --write && git diff --exit-code openapi/` → exit 0 (spec is committed and current).
- T1.1.b `uv run pytest tests/test_contract.py::test_spec_file_matches_app` → passes (same check as T1.1.a, in CI, like the DDL test).
- T1.1.c Baseline recorded. With Redocly's built-in `recommended` set (no config, as in 1.1), `openapi/v1.yaml` gives exactly 13 errors and 4 warnings. With the repo's `redocly.yaml` (`scripts/verify/lint.sh`, Redocly pinned at 2.59.0) the same spec gives 17 errors and 0 warnings, because the config raises `operation-4xx-response` and `info-license` to errors. Later phases count against the `lint.sh` number. This test is "expected to fail" and passes when both counts match.

**P1.2 Breaking-change detector.**
Add `scripts/verify/breaking.sh` running `oasdiff breaking <base> openapi/v1.yaml --fail-on ERR`
(via `docker run --rm tufin/oasdiff@sha256:…`, pinned, so nothing is installed on the host), where
`<base>` is `openapi/v1.yaml` at a git ref (default `origin/main`; `HEAD` for scratch checks).
- T1.2.a Removing a field from a response schema in a scratch commit → `breaking.sh` exits 1 and names the field.
- T1.2.b Adding an optional query parameter in a scratch commit → `breaking.sh` exits 0.

**P1.3 Contract test harness.**
Add `schemathesis` to the dev group and `tests/test_contract.py` that runs
`schemathesis.openapi.from_asgi("/openapi.json", app)` over every operation, checking
`not_a_server_error`, `status_code_conformance`, `content_type_conformance`,
`response_schema_conformance`. Two modes: **pg** (real Postgres with every register's
fixture rows, when `REGISTERWATCH_TEST_DATABASE_URL` is set) and **fake** (an empty fake
connection under the real query code). The engine is always faked. Generation is seeded
and deterministic, and mixes real jurisdiction codes, slugs and tables into path
parameters so requests reach the query code.
Known failures are pinned per mode in `tests/contract_baseline.json`. A new failure
fails the suite, and so does a pinned failure that no longer occurs, so the file
only shrinks. `REGISTERWATCH_CONTRACT_BASELINE=write` regenerates it and prints one
failing request per entry. **Every later phase that fixes a finding removes its entries
from this file in the same commit.**
- T1.3.a `uv run pytest tests/test_contract.py` (both modes): the conformance test runs for every operation in the spec (the last test asserts the set of operations run equals the spec's paths × methods, 12 today) and passes against the baseline.
- T1.3.b **[pg]** Recorded as baseline: on today's code it reports the negative-`offset` 500 (F2). Evidence line quotes the failing example. It also reports the `/changes` negative-`limit` 500 (F2) and the NUL-byte `/search` 500 (F18).

Checklist
- [x] ~~T1.1.a spec written and committed, diff clean~~
- [x] ~~T1.1.b spec-vs-app test passes in pytest~~
- [x] ~~T1.1.c baseline lint recorded: 13 errors, 4 warnings (17 errors with redocly.yaml)~~
- [x] ~~T1.2.a breaking detector catches a removed field~~
- [x] ~~T1.2.b breaking detector allows an additive change~~
- [x] ~~T1.3.a schemathesis covers every operation~~
- [x] ~~T1.3.b baseline 500 on negative offset recorded~~
- [x] ~~GATE P1~~

---

### Phase 2. Versioned routing, operation ids, typed responses

Depends on: P1. Fixes F6, F8 (paths), F9, F11, F15.

**P2.1 `/v1` router and module split.**
New package `src/registerwatch/http/`: `deps.py` (transaction, read check, identifier
lookup), `models.py` (every v1 schema), `cursor.py` (opaque cursors), `openapi.py`, and
`v1/{jurisdictions,registers,search,domains}.py`, mounted from `api.py` at `/v1`.
The legacy routes stay in `api.py` byte-for-byte: `tests/test_api.py` patches names
on that module, so moving them would have meant changing the tests that prove they are
unchanged. Phase 12 deletes them from there. Modules for changes and snapshots (P4),
ingest runs (P5), and status and probes (P8) arrive with their phases.
Import-linter contract: `registerwatch.http` may not import `registerwatch.api` or
`registerwatch.cli`; register parsers may not import `registerwatch.http`.
Phase 2's v1 surface: `listJurisdictions`, `getJurisdiction`, `listRegisters`,
`getRegister`, `getTable`, `listRows`, `search`, `getDomainStatus`. `listRows` already
uses the final contract: `filter[col]=` filters, unknown parameters refused, and an
opaque, query-bound `cursor`. Inside, the cursor still holds an offset until P4.2 makes it
a keyset; clients cannot tell.
- T2.1.a `uv run pytest tests/test_api.py` (existing tests, unchanged) passes against the legacy routes, and the legacy paths and their schemas in `openapi/v1.yaml` are identical to Phase 1's (`breaking.sh` against the Phase 1 commit passes too).
- T2.1.b `uv run lint-imports` passes, including the new contract.
- T2.1.c `GET /v1/jurisdictions`, `/v1/registers`, `/v1/registers/gb_ukgc`, `/v1/registers/gb_ukgc/tables/licences` return 200 with the shapes in 2.2 (new tests in `tests/test_api_v1.py`).

**P2.2 Explicit operation ids and tags.**
`operationId` = lowerCamel verb+noun (`listJurisdictions`, `getJurisdiction`,
`listRegisters`, `getRegister`, `getTable`, `listRows`, `getRow`, `listRegisterChanges`,
`listJurisdictionChanges`, `listSnapshots`, `search`, `getDomainStatus`, `createIngestRun`,
`listIngestRuns`, `getIngestRun`, `getStatus`, `livez`, `readyz`). Tags: `jurisdictions`,
`registers`, `rows`, `changes`, `search`, `domains`, `ingest`, `operations`.
- T2.2.a `test_contract.py::test_operation_ids_are_explicit` asserts no v1 `operationId` contains `_` and all ids are unique. Legacy ids stay as generated, since renaming them would rename methods in clients already generated from them.
- T2.2.b Every v1 operation has exactly one tag from the list above.

**P2.3 Pydantic response models with examples.**
One model per resource (`Jurisdiction`, `RegisterSummary`, `Register`, `TableSchema`,
`Row` (fixed fields `id`, `first_seen_at`, `last_seen_at`, `first_seen_snapshot_id`,
plus `values: dict[str, Any]` validated against the table's declared columns),
`Change`, `Snapshot`, `SearchHit`, `DomainStatus`, `IngestRun`, `IngestRunResult`,
`Status`, `Page[T]`). Every model has `json_schema_extra` examples taken from real
fixture rows (gb_ukgc, ch_esbk).
Built in Phase 2: the models for the Phase 2 surface. `Row` has `first_seen_at`,
`last_seen_at` and `values`; `id` and `first_seen_snapshot_id` are added (additively)
when P4.1 exposes them in the views. Pages are explicit classes (`RowPage`, ...) rather
than `Page[T]`, so schema names stay readable. `Change`, `Snapshot`, `IngestRun*` and
`Status` arrive with their endpoints. FastAPI strips `null` from examples when it writes
the spec; `http/openapi.py` restores them so examples still match their schemas.
- T2.3.a `test_contract.py::test_no_untyped_200s`: no v1 2xx response schema is `{"type":"object","additionalProperties":true}`.
- T2.3.b Every v1 2xx response in the spec has at least one example.
- T2.3.c `schemathesis` `response_schema_conformance` passes for every v1 read operation (fake and [pg]), and a deliberately wrong schema (`Row.values: integer`) fails it, so the check is not vacuous.

**P2.4 Spec metadata.**
`servers`, `info.license` (match the repo's licence; if none, add one in this step and
say which), `info.contact`.
As built: `servers` is the relative URL `/` ("the deployment serving this document").
No deployment host is known, the `api.registerwatch.dev` name first written here was a
placeholder, and `localhost` trips Redocly's `no-server-example.com`. The repo has no
LICENSE file, so `info.license` says "All rights reserved" (linking to an explanation
of what no licence means) until the owner picks one. `info.contact` is the repository.
- T2.4.a Redocly lint: `no-empty-servers`, `info-license` errors gone.

Checklist
- [x] ~~T2.1.a legacy tests still pass~~
- [x] ~~T2.1.b import contracts pass~~
- [x] ~~T2.1.c v1 discovery endpoints return documented shapes~~
- [x] ~~T2.2.a operation ids explicit and unique~~
- [x] ~~T2.2.b one tag per operation~~
- [x] ~~T2.3.a no untyped 2xx schemas~~
- [x] ~~T2.3.b every 2xx has an example~~
- [x] ~~T2.3.c schemathesis schema conformance on v1 reads~~
- [x] ~~T2.4.a servers and license lint errors gone~~
- [x] ~~GATE P2~~

---

### Phase 3. Errors (RFC 9457) and input validation

Depends on: P2. Fixes F2 (validation), F7, F17.

**P3.1 Problem Details everywhere.**
Exception handlers for `HTTPException`, `RequestValidationError`, `psycopg_pool.PoolTimeout`,
`psycopg.errors.QueryCanceled`, and `Exception`, all producing `application/problem+json`
with the 2.5 catalog. As built: `PoolTimeout` is caught as its base, `psycopg.OperationalError`
(connection failures too); `Exception` is caught by a middleware inside the request-id one,
because Starlette's server-error handler sits outside every user middleware and its 500 would
carry no `X-Request-Id`. Legacy routes keep their bodies; the handlers reformat only `/v1`. `RequestValidationError` maps to 400 `invalid-parameter` with
`errors[]` (`{"field": "limit", "message": "must be ≤ 1000"}`). The catalog lives in one
module (`http/problems.py`) as an enum, and the spec's `components.responses` are
generated from it.
- T3.1.a `tests/test_errors.py` parametrised over every catalog entry triggers it and asserts status, `Content-Type: application/problem+json`, `type` equals the catalog URI, `instance` equals the request path and `request_id` the echoed `X-Request-Id`. Entries whose feature lands later (`row-not-found` P4; `ingest-*`, `unsupported-media-type`, `idempotency-key-reused` P5; `forbidden` P6; `rate-limited` P7) are raised through a real v1 route by a patch until that phase adds the real trigger.
- T3.1.b A forced unhandled exception returns 500 `internal` with no traceback text in the body (assert `"Traceback"` and `".py"` not in body).
- T3.1.c Every v1 operation documents 400 (if it has params), 401 (if secured), 404 (if it has path ids), 429, 500, 503 in the spec (`test_contract.py::test_error_responses_documented`).
- T3.1.d Redocly `operation-4xx-response` warnings: 0. As built: the three legacy routes without a 4xx (`/health`, `/registers`, `/status`) now document the 400 the NUL guard (below) gives on every path, which is true, so nothing is suppressed.

**P3.2 Bounded, validated parameters.**
`limit: Annotated[int, Query(ge=1, le=1000)]` everywhere (search per-table `le=100`),
`offset` removed from v1, `since`/`until` as `AwareDatetime`, `until > since`,
`q` `min_length=2, max_length=200`, `domain` validated with `extract.host()` and
`max_length=253`, `jurisdiction` as a repeated param validated against known codes.
As built, also: a query parameter an operation does not declare is a 400 on every v1
operation (a typo like `?jurisdictions=gb` must not silently return everything); an unknown
code in `jurisdiction` is a 400 `invalid-parameter`, not a 404, since it filters rather than
names the resource; a NUL byte anywhere in the path or query is a 400 on every path, legacy
included (F18; a middleware, so it never reaches Postgres). The legacy 500s T1.3.b pinned were
negative and out-of-range `limit`/`offset` and NUL bytes; legacy `offset ≥ 0` and changes
`limit ≥ 0`, both at most Postgres' bigint, refuse only values that used to be 500s.
`breaking.sh` against the Phase 2 commit reports these four bounds and v1 `domain`
`maxLength: 253` as narrowings; v1 is unreleased (main has no spec yet), so the gate against
`origin/main` passes.
- T3.2.a `GET /v1/registers/gb_ukgc/tables/licences/rows?limit=0`, `limit=1001`, `limit=-1` → 400 `invalid-parameter`, `errors[0].field == "limit"`.
- T3.2.b Every v1 datetime parameter refuses a naive value with 400. No v1 operation takes one until the change feed lands in P4.3, so the concrete check `GET /v1/registers/gb_ukgc/changes?since=2026-10-01T00:00:00` → 400 (and with `Z` → 200) runs there as T4.3.d. P3 adds the shared `AwareDatetime` parameter type and its unit test.
- T3.2.c `GET /v1/domains/not_a_host!!` → 400; a 300-char domain → 400.
- T3.2.d `GET /v1/search?q=a` → 400; `q` of 201 chars → 400.
- T3.2.e schemathesis `not_a_server_error` passes on every v1 operation (the T1.3.b baseline failures are gone and their entries are removed from `tests/contract_baseline.json`). The one `not_a_server_error` left in the fake-mode baseline is legacy `/status`'s deliberate 503 (F13, Phase 8).
- T3.2.f **[pg]** `GET /v1/search?q=a%00b` and `filter[status]=a%00b` on rows → 400 `invalid-parameter`, not 500 (F18).

**P3.3 Request id.**
Middleware: accept a valid `X-Request-Id` (≤ 128 printable chars) or generate a UUIDv7;
echo it on every response; include it in log records.
- T3.3.a Response to a request without the header carries a UUIDv7 `X-Request-Id`.
- T3.3.b A supplied id is echoed; a 1 KB id is replaced, not echoed.

Checklist
- [x] ~~T3.1.a every catalog entry returns a correct problem~~
- [x] ~~T3.1.b 500s leak no traceback~~
- [x] ~~T3.1.c error responses documented per operation~~
- [x] ~~T3.1.d no 4xx lint warnings~~
- [x] ~~T3.2.a limit bounds enforced~~
- [x] ~~T3.2.b naive datetimes refused~~
- [x] ~~T3.2.c domain validated~~
- [x] ~~T3.2.d q length validated~~
- [x] ~~T3.2.e schemathesis finds no 5xx~~
- [x] ~~T3.2.f NUL bytes refused with 400~~
- [x] ~~T3.3.a request id generated~~
- [x] ~~T3.3.b request id echoed or replaced~~
- [x] ~~GATE P3~~

---

### Phase 4. Stable pagination and a complete change feed

Depends on: P3. Fixes F1, F2 (offset), F4, F10. Needs a schema migration.

**P4.1 Expose a stable key.**
Regenerate DDL so `current_<table>` views include `id` (and `first_seen_snapshot_id`).
Add `CREATE INDEX ... (id) WHERE removed_snapshot_id IS NULL` per table (the PK covers
it for the keyset; the partial index keeps the scan on current rows).
`uv run registerwatch ddl --write`, mirror into `supabase/migrations/`.
As built: the regenerated DDL is a new migration version, `20261007000007_register_schemas.sql`,
not an edit of `…000005`: Supabase applies each version once, so an edited file would never
reach it. `…000005` stays in `supabase/migrations/` as history; the package ships only the
newest, which upgrades any older one because view columns are only ever appended (`id`,
`first_seen_snapshot_id` go last, so `CREATE OR REPLACE VIEW` accepts them). Legacy row
responses and `registerwatch rows` read the views with `SELECT *`, so they gain the two
fields too (additive; their order and paging are unchanged).
- T4.1.a `tests/test_schema.py` (DDL vs committed migration) passes.
- T4.1.b **[pg]** `test_postgres.py` applies all migrations twice (idempotent) and `SELECT id FROM gb_ukgc.current_licences LIMIT 1` works. Also: `supabase/migrations/` applied in order to an empty database (the Supabase upgrade path) ends with the new views and index.
- T4.1.c **[pg]** `EXPLAIN (FORMAT JSON)` of the page query for `gb_ukgc.licences` with `WHERE id > $1 ORDER BY id LIMIT 100` shows an index scan, not a sort over the table. As built: the query is the one `query.row_page` actually sends; at the latest snapshot it uses the partial index, and read as of an older snapshot (below) the primary key.

**P4.2 Cursor pagination for rows.**
Implement the cursor in 2.3. Filters move to `filter[<column>]=<value>` (FastAPI: parse
`request.query_params` keys matching `^filter\[([a-z][a-z0-9_]*)\]$`); any other unknown
query parameter is a 400 `invalid-parameter`.
As built: the cursor holds the last `id` and the snapshot the first page was read at, and
every later page reads the table as of that snapshot (`first_seen ≤ s < removed`), so a walk
is exactly one snapshot even when an ingest lands mid-walk. Its tag is a checksum over the
query and the position, so a cursor from another query or with any byte altered is a 400.
The `Link` target is a path-absolute reference (`</v1/...?cursor=...>`), like the `url` fields.
- T4.2.a **[pg]** Walk every page of `gb_ukgc.licences` with `limit=37`: the union of ids equals `SELECT id FROM current_licences`, no duplicates, no gaps.
- T4.2.b **[pg]** Walk half the pages, run an ingest that adds 3 rows and removes 2 (the existing three-run engine test data), finish the walk: no row returned twice, no row present in both snapshots skipped. As built: the ingest is its write step (`observations.apply` with a new complete snapshot) on the loaded 399-row table, since the engine's test data has 4 licence rows; the finished walk equals the starting snapshot exactly, and a fresh walk shows the new one.
- T4.2.c A cursor reused with a different `filter[...]` → 400 `invalid-cursor`. A cursor with a flipped byte → 400 `invalid-cursor`.
- T4.2.d `?status=Active` (old style) on a v1 route → 400 naming `status` and suggesting `filter[status]`.
- T4.2.e `Link: <...>; rel="next"` present iff `has_more`.
- T4.2.f `GET /v1/registers/gb_ukgc/tables/licences/rows/{id}` (`getRow`) returns the row with its history markers; an unknown id → 404. `Row` gains `id` and `first_seen_snapshot_id` in every response.

**P4.3 Change feed.**
`/v1/registers/{slug}/changes` and `/v1/jurisdictions/{code}/changes` return the flat
event list, cursor on `(snapshot_id, id, change)`. Baseline snapshot excluded, as today.
As built: the order and cursor are `(snapshot_id, register, table, id, change)`, since ids are
per table; within a snapshot a changed row's `removed` comes first (the old row's id is lower).
`since`/`until` bound the recorded time, both optional, `until > since`.
- T4.3.a **[pg]** After the engine's three-run test: the feed since run 1 returns exactly ~~1 `added` and 1 `removed` event~~ run 2's 2 `added` and 2 `removed` events (it revokes 103 and renumbers 102, each a removal plus an addition; the count first written here was wrong), in snapshot order.
- T4.3.b **[pg]** With 2,500 synthetic changes and `limit=1000`: three pages, `has_more` true, true, false; 2,500 events total (F4 regression test).
- T4.3.c Jurisdiction feed for `ch` interleaves `ch_esbk` and `ch_gespa` events in `(snapshot_id, id)` order.
- T4.3.d `GET /v1/registers/gb_ukgc/changes?since=2026-10-01T00:00:00` (naive) → 400; with `Z` → 200 (moved here from T3.2.b).

**P4.4 Search pagination.**
`/v1/search` returns one hit per table with `total` and the first `limit` rows, plus a
`rows_url` for the full, paged result (`.../rows?q=...`). Tables are searched with one
`UNION ALL` query per register rather than two queries per table.
As built: `rows_url` adds `include_total=true`, so its `total` is there to compare. Legacy
`/search` keeps its per-table queries until it is removed.
- T4.4.a Query count per `/v1/search` call (instrumented via a psycopg connection wrapper in the test) ≤ number of registers searched.
- T4.4.b Every `rows_url` in a search response returns 200 and the same `total`.

**P4.5 Snapshot history.**
`GET /v1/registers/{slug}/snapshots` (`listSnapshots`): every recorded run, complete or
not, newest first, with `complete`, `incomplete_reason`, `record_count`, `fetched_at`,
keyset-paged on `id`.
- T4.5.a **[pg]** After the engine's three-run test: three snapshots, newest first; the incomplete one carries its reason.
- T4.5.b **[pg]** `limit=1` walks all three in three pages, `has_more` false on the last.

Checklist
- [x] ~~T4.1.a DDL test passes~~
- [x] ~~T4.1.b migrations idempotent, views expose id~~
- [x] ~~T4.1.c keyset page uses an index scan~~
- [x] ~~T4.2.a full walk: no duplicates, no gaps~~
- [x] ~~T4.2.b walk across an ingest stays consistent~~
- [x] ~~T4.2.c bad or mismatched cursor refused~~
- [x] ~~T4.2.d old filter style refused with hint~~
- [x] ~~T4.2.e Link header matches has_more~~
- [x] ~~T4.2.f getRow returns one row by id~~
- [x] ~~T4.3.a change feed matches engine history~~
- [x] ~~T4.3.b large feed pages without truncation~~
- [x] ~~T4.3.c jurisdiction feed ordered across registers~~
- [x] ~~T4.3.d naive since refused on the change feed~~
- [x] ~~T4.4.a search query count bounded~~
- [x] ~~T4.4.b search rows_url consistent~~
- [x] ~~T4.5.a snapshot history newest first with reasons~~
- [x] ~~T4.5.b snapshot history pages~~
- [x] ~~GATE P4~~

---

### Phase 5. Ingest runs as a persisted resource

Depends on: P3. Fixes F3, F8 (ingest), F14 (`wait`).

**P5.1 `ingest_runs` table.**
Migration `migrations/2026101000000x_ingest_runs.sql` (plain Postgres, mirrored):
`ingest_runs(id uuid pk, status text check in ('queued','running','succeeded','partial','failed'), requested jsonb, idempotency_key text unique null, request_hash bytea, created_at, started_at, finished_at, error text)` and
`ingest_run_results(run_id fk, slug, snapshot_id fk null, complete, skipped, reason)`.
RLS on and grants revoked, as in `20261004000006_lock_down_public.sql`.
As built: `20261007000008_ingest_runs.sql` (the next version after Phase 4's `…000007`).
`ingest_runs` also stores `registers` (what the request resolved to, in run order) and
`not_started`; `ingest_run_results` also stores `position`, `unchanged`, `record_count` and
`finished_at`. The key is unique while set; the API clears keys older than 24 h before it looks.
- T5.1.a **[pg]** Migration applies on a database already at today's head, and twice.
- T5.1.b **[pg]** As role `anon` (created in the test), `SELECT * FROM ingest_runs` is denied.

**P5.2 Cross-process single-flight.**
Replace `threading.Lock` with `pg_try_advisory_lock(<constant>)` held on a dedicated
connection for the run's duration (session-level, released on disconnect, so a crashed
replica frees it).
As built: the advisory lock is added, not swapped in. The `threading.Lock` stays, shared with
the legacy `/ingest` routes until Phase 12, so legacy and v1 runs never overlap in one process;
across processes only the advisory lock decides. The sweep runs at startup and again whenever
the lock is taken, since only the lock's holder can know an active row has lost its worker.
- T5.2.a **[pg]** Two app instances (as run: the second is a separate Python process, so it has its own `threading.Lock` and only the advisory lock can refuse it): the second `POST /v1/ingest-runs` while the first runs → 409 `ingest-in-progress` with `active_run` pointing at the first.
- T5.2.b **[pg]** Kill the connection holding the lock (`pg_terminate_backend`) → the next POST is accepted; the orphaned run is marked `failed` with `error='worker lost'` by a startup sweep.

**P5.3 Endpoints.**
`POST /v1/ingest-runs` → 202, `Location: /v1/ingest-runs/{id}`, body = the run.
Body validated: exactly one of `registers` or `jurisdiction`, or neither for all.
`Idempotency-Key` (≤ 255 chars): same key + same body within 24 h returns the original
run (200), same key + different body → 422. `GET /v1/ingest-runs` (cursor by
`created_at desc, id`), `GET /v1/ingest-runs/{id}`. `?wait` is not carried to v1.
As built: T5.3.a, b, c and f are **[pg]**: with runs held in memory they would prove nothing
about F3. T5.3.d and e run without a database (they are refused before it is reached).
- T5.3.a **[pg]** POST returns 202, `Location` resolves to 200 with `status` in (`queued`,`running`); after the fake engine finishes, `status == 'succeeded'` and per-register results are present.
- T5.3.b **[pg]** Same `Idempotency-Key` twice → one run created; second response 200 with the same id.
- T5.3.c **[pg]** Same key, different body → 422 `idempotency-key-reused`.
- T5.3.d `{"registers": ["xx_nope"]}` → 404 `register-not-found`; `{"registers": [], "jurisdiction": "gb"}` → 400.
- T5.3.e Read token on POST → 403; no token → 401 with `WWW-Authenticate`.
- T5.3.f **[pg]** After an app restart (new `TestClient`), `GET /v1/ingest-runs/{id}` of a finished run still returns it (F3 regression).

**P5.4 Graceful shutdown of a running batch.**
On SIGTERM, stop starting new registers, let the current register finish (bounded by
`SHUTDOWN_GRACE_S`, default 60), mark the run `partial` with the unstarted registers listed.
As built: the app side is a `STOP` event set by a SIGTERM handler that then hands the signal
on to uvicorn; uvicorn's graceful shutdown waits for the background run. The bound
(`--timeout-graceful-shutdown`, `SHUTDOWN_GRACE_S`) is set where the server is started, which
P9.3 owns. A run still going when the bound kills the process is left `running` and the next
holder's sweep fails it (`worker lost`), as in T5.2.b.
- T5.4.a Unit: the engine loop checks a stop event between registers (fake engine with 5 registers, stop after 2 → run `partial`, results for 2, `not_started` lists 3).
- T5.4.b Covered at OS level in T9.3.b.

Checklist
- [x] ~~T5.1.a ingest_runs migration idempotent~~
- [x] ~~T5.1.b ingest_runs hidden from anon~~
- [x] ~~T5.2.a single flight across two instances~~
- [x] ~~T5.2.b lock freed when its holder dies~~
- [x] ~~T5.3.a 202 + Location + final state~~
- [x] ~~T5.3.b idempotent replay~~
- [x] ~~T5.3.c key reuse with new body refused~~
- [x] ~~T5.3.d bad targets refused~~
- [x] ~~T5.3.e 401 vs 403 correct~~
- [x] ~~T5.3.f run survives restart~~
- [x] ~~T5.4.a stop between registers marks partial~~
- [x] ~~GATE P5~~

---

### Phase 6. Security schemes and authorization

Depends on: P2. Fixes F5.

**P6.1 Declare security.**
`HTTPBearer` dependencies named `ReadToken` and `IngestToken`; global `security` for
read routes, `IngestToken` on ingest routes, `security: []` on `/livez`, `/readyz`,
`/v1/status`. The `authorization` header parameter disappears from the spec.
As built: FastAPI has no global `security`, so every operation carries its own: read routes
`[ReadToken, IngestToken]` (either; the ingest token reads), ingest routes `[IngestToken]`.
`/livez`, `/readyz` and `/v1/status` do not exist until Phase 8; today's open routes are the
legacy `/health`, `/registers` and `/status`, which get `security: []`. Phase 8 adds its probes
to the open set in `test_no_authorization_header_param`. Legacy routes declare the schemes too
(the lint covers the whole spec), but their checks still compare the raw header and answer
`{"detail": ...}`. Dropping the header parameter left legacy `/jurisdictions` and `/ingest/last`
with no 4xx (their 422 came from it), so they now document the NUL 400 they already answer.
The 401 response component documents `WWW-Authenticate`.
- T6.1.a Redocly `security-defined` errors: 0.
- T6.1.b `test_contract.py::test_no_authorization_header_param` passes.

**P6.2 Correct status codes and headers.**
- T6.2.a Missing token on a read route (with `READ_TOKEN` set) → 401, `WWW-Authenticate: Bearer realm="registerwatch"`.
- T6.2.b Wrong token → 401 with `error="invalid_token"` in `WWW-Authenticate` (RFC 6750).
- T6.2.c Read token on ingest → 403 `forbidden`.
- T6.2.d Token comparison stays constant-time: `test_auth_uses_compare_digest` patches `secrets.compare_digest` and asserts it was called for every candidate token.
- T6.2.e Tokens never appear in logs: run the auth tests with `caplog` at DEBUG and assert neither token string occurs in any record.

As built: tokens are compared as bytes. `compare_digest` raises `TypeError` on a non-ASCII str,
so a Latin-1 `Authorization` header was a 500 on every authenticated route, legacy included; it
is now a 401. A request with no bearer token (none, another scheme, `Bearer` alone) gets the
bare challenge; `error="invalid_token"` is only for a token that was sent (RFC 6750 3.1).

**P6.3 Minimum token strength.**
Startup refuses an `INGEST_TOKEN` or `READ_TOKEN` shorter than 32 characters.
- T6.3.a `tests/test_config.py`: a 10-char token raises at `settings()`; a 43-char `token_urlsafe(32)` loads.
As built: `Settings` also sets `hide_input_in_errors`, so the refused value is not repeated in
the startup error (pydantic prints `input_value=` by default, which would put a token in the logs).

Checklist
- [ ] T6.1.a security-defined errors gone
- [ ] T6.1.b no authorization header param in spec
- [ ] T6.2.a 401 with WWW-Authenticate
- [ ] T6.2.b invalid_token on wrong token
- [ ] T6.2.c 403 for read token on ingest
- [ ] T6.2.d constant-time comparison
- [ ] T6.2.e tokens absent from logs
- [ ] T6.3.a short tokens refused at startup
- [ ] GATE P6

---

### Phase 7. Caching, rate limiting, query cost

Depends on: P4. Fixes F12, F14 (statement timeout).

**P7.1 ETags and Cache-Control.**
Data version per register = newest complete snapshot id (one indexed query, cached in
process for 30 s). ETag = `W/"<sha256 of sorted (slug, snapshot_id) + request path + query>"`.
- T7.1.a `GET` rows twice with `If-None-Match` from the first → 304, empty body, same `ETag`.
- T7.1.b **[pg]** After a complete ingest of that register → 200 with a new ETag. After an incomplete ingest → still 304.
- T7.1.c `Cache-Control: public, max-age=300` when `READ_TOKEN` is empty; `private, max-age=300` when set. `Vary: Authorization, Accept-Encoding` always.
- T7.1.d Ingest and status responses carry `Cache-Control: no-store`.

**P7.2 Rate limits.**
Token-bucket per key (token hash, or client IP from the trusted proxy header, see P10.2),
kept in process. Limits are per replica, which is acceptable at this scale and is
documented as such; a shared store is not needed. Limits as in 2.3, configurable.
- T7.2.a 61 `GET /v1/search` in a minute from one key → the 61st is 429 `rate-limited` with `Retry-After` ≥ 1.
- T7.2.b Every response carries `RateLimit-Policy` and `RateLimit` headers whose remaining value decreases by 1 per request.
- T7.2.c Two different tokens have independent buckets.
- T7.2.d `/livez` and `/readyz` are never limited (1,000 requests, all 200).

**P7.3 Query cost controls.**
`SET LOCAL statement_timeout = '5s'` in read transactions (`READ_STATEMENT_TIMEOUT_MS`);
`pg_trgm` GIN indexes on the text columns used by `q`, generated by `ddl` behind a
per-column `searchable=True` flag on the register declaration; a `lower(host)` btree and
a `reverse(lower(host)) text_pattern_ops` index for the subdomain match in `/v1/domains`
(rewrite `LIKE '%.x'` as `reverse(lower(host)) LIKE reverse('.x') || '%'`).
- T7.3.a **[pg]** `EXPLAIN` of the `q` query on `gb_ukgc.trading_names` uses a bitmap index scan on the trigram index.
- T7.3.b **[pg]** `EXPLAIN` of the domain query on `gb_ukgc.domain_names` uses the reverse index (no seq scan).
- T7.3.c **[pg]** `SELECT pg_sleep(10)` through the read path → 504 `query-timeout` after ~5 s (assert elapsed < 6 s).
- T7.3.d **[pg]** p95 latency of `/v1/search?q=bet` over 50 calls on the fixture-loaded DB < 300 ms (recorded number in evidence).

Checklist
- [ ] T7.1.a 304 on matching ETag
- [ ] T7.1.b ETag changes only on a complete ingest
- [ ] T7.1.c Cache-Control and Vary correct
- [ ] T7.1.d no-store on ingest and status
- [ ] T7.2.a 429 with Retry-After
- [ ] T7.2.b RateLimit headers count down
- [ ] T7.2.c buckets per token
- [ ] T7.2.d probes never limited
- [ ] T7.3.a trigram index used by q
- [ ] T7.3.b reverse index used by domain match
- [ ] T7.3.c statement timeout returns 504
- [ ] T7.3.d search p95 under 300 ms recorded
- [ ] GATE P7

---

### Phase 8. Probes, status, and the database pool

Depends on: P3. Fixes F13, F14.

**P8.1 Split liveness, readiness and status.**
`/livez` (no I/O), `/readyz` (`SELECT 1` with a 2 s pool timeout; 503 `database-unavailable`
otherwise), `/v1/status` (always 200 with `stale`), `/v1/status?strict=true` (503 when
stale, the old behaviour for uptime monitors).
- T8.1.a DB faked down: `/livez` 200, `/readyz` 503 problem, `/v1/status` 200 with `stale: true` and `database: "unreachable"`, `/v1/status?strict=true` 503.
- T8.1.b One stale register: `/readyz` 200, `/v1/status` 200 naming it, `?strict=true` 503.
- T8.1.c The existing three `/status` tests pass unchanged against the legacy route.

**P8.2 Pool sizing and starvation behaviour.**
Pool `max_size` from `DB_POOL_MAX` (default 10), `timeout` from `DB_POOL_TIMEOUT_S`
(default 3); ingest uses its own pool (`max_size=2`) so a batch cannot starve reads;
uvicorn/anyio thread limiter set to `DB_POOL_MAX * 2`. `PoolTimeout` → 503
`database-unavailable` with `Retry-After: 5`.
- T8.2.a **[pg]** With `DB_POOL_MAX=2`, 10 concurrent `pg_sleep(1)` reads: every response is 200 or 503 problem (none 500), and no response takes longer than `1 s × ceil(10/2) + DB_POOL_TIMEOUT_S`.
- T8.2.b **[pg]** During a running ingest (fake engine holding its pool connection), 20 sequential reads all succeed.
- T8.2.c **[pg]** `SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'registerwatch%'` never exceeds `DB_POOL_MAX + 2 + 1` (reads + ingest + advisory lock) during T8.2.a. Set `application_name` in the pool kwargs for this.

Checklist
- [ ] T8.1.a probes behave with DB down
- [ ] T8.1.b probes behave with a stale register
- [ ] T8.1.c legacy status tests unchanged
- [ ] T8.2.a pool exhaustion is 503, not 500
- [ ] T8.2.b ingest cannot starve reads
- [ ] T8.2.c Postgres connection count bounded
- [ ] GATE P8

---

### Phase 9. OS and container level

Depends on: P8. Fixes F16 (container). All tests are in `scripts/verify/os.sh` and
run against the locally built image. Build and run:

```bash
docker build -t registerwatch:plan .
docker run -d --name rw-api --network host --read-only --tmpfs /tmp \
  -e DATABASE_URL=postgresql://postgres:rw@localhost:55432/rw \
  -e INGEST_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') \
  -e USER_AGENT='RegisterWatch/plan (+https://registerwatch.dev/bot; ops@registerwatch.dev)' \
  -e BLOB_BACKEND=local -e SNAPSHOT_ROOT=/tmp/snapshots \
  registerwatch:plan
```

**P9.1 Non-root, read-only, minimal image.**
Dockerfile: multi-stage (build with uv, run from `python:3.12-slim-bookworm` with only
the venv copied), `USER 10001:10001`, no shell tools beyond the base, `HEALTHCHECK`
on `/livez` using Python (`python -c "import urllib.request,sys;urllib.request.urlopen('http://127.0.0.1:8000/livez',timeout=2)"`).
- T9.1.a **[box]** `docker exec rw-api id -u` → `10001`.
- T9.1.b **[box]** The container starts and serves `/livez` with `--read-only --tmpfs /tmp` (no writes outside `/tmp`).
- T9.1.c **[box]** `docker inspect --format '{{.State.Health.Status}}' rw-api` → `healthy` within 30 s.
- T9.1.d **[box]** Image size ≤ 250 MB (`docker image inspect -f '{{.Size}}'`); record the before/after numbers.
- T9.1.e **[box]** `docker run --rm --entrypoint python registerwatch:plan -c "import registerwatch, importlib.resources as r; print((r.files('registerwatch')/'registers/certs/disig_r2i2.pem').is_file())"` → `True` (package data survived the multi-stage copy).

**P9.2 Process model and limits.**
`registerwatch serve` gains `--workers` (default 1; `WEB_CONCURRENCY`), `--timeout-keep-alive`
(default 75 s), `--timeout-graceful-shutdown` (default `SHUTDOWN_GRACE_S + 5`),
`--limit-concurrency` (default 200), `--proxy-headers --forwarded-allow-ips` from
`FORWARDED_ALLOW_IPS`. PID 1 is the Python process (exec-form `CMD`), so it receives SIGTERM.
- T9.2.a **[box]** `docker exec rw-api cat /proc/1/cmdline | tr '\0' ' '` shows the registerwatch/uvicorn process as PID 1 (no shell wrapper).
- T9.2.b **[box]** `docker exec rw-api sh -c 'cat /proc/1/limits'` (or Python equivalent if no shell) shows `Max open files` ≥ 4096; if the platform default is lower, the image sets it via the entrypoint and the test checks the raised value.
- T9.2.c **[box]** With `--limit-concurrency 5`, 20 concurrent slow requests: excess requests get 503 quickly (< 100 ms), none hang.

**P9.3 Signals and shutdown.**
- T9.3.a **[box]** `time docker stop -t 30 rw-api` with no work in flight completes in < 3 s, exit code 0 (`docker inspect -f '{{.State.ExitCode}}'`).
- T9.3.b **[box][pg]** Start an ingest run with a register patched to sleep 20 s per register (test-only `REGISTERWATCH_FAKE_SLOW_INGEST=1`), `docker stop -t 90`: the container exits within `SHUTDOWN_GRACE_S + 10` s and `GET /v1/ingest-runs/{id}` (after restart) shows `partial` with `not_started` populated, and `pg_locks` shows no leftover advisory lock.
- T9.3.c **[box]** In-flight read requests during `docker stop` complete with 200 (send 10 slow reads, stop, all 10 return).

**P9.4 Memory and file descriptors under load.**
- T9.4.a **[box]** After `scripts/verify/load.py --rps 50 --duration 120` against `/v1/registers/gb_ukgc/tables/licences/rows?limit=1000`, RSS from `docker stats --no-stream` grows < 20 % between minute 1 and minute 2 (no leak), and `ls /proc/1/fd | wc -l` returns to within 10 of its idle value 10 s after the load ends.

Checklist
- [ ] T9.1.a runs as uid 10001
- [ ] T9.1.b works on a read-only root fs
- [ ] T9.1.c Docker healthcheck healthy
- [ ] T9.1.d image size recorded and ≤ 250 MB
- [ ] T9.1.e package data present in the image
- [ ] T9.2.a app is PID 1
- [ ] T9.2.b open-file limit ≥ 4096
- [ ] T9.2.c concurrency limit sheds load fast
- [ ] T9.3.a idle stop under 3 s, exit 0
- [ ] T9.3.b stop during ingest marks run partial, lock freed
- [ ] T9.3.c in-flight reads finish on stop
- [ ] T9.4.a no memory or fd leak under load
- [ ] GATE P9

---

### Phase 10. Network level

Depends on: P9. Fixes F16 (network). Tests in `scripts/verify/net.sh`, run against
`$RW_BASE` (local container for **[box]**, staging for **[edge]**).

**P10.1 Transport security at the edge.**
TLS terminates at the platform proxy (Railway/Fly/Render). The app adds
`Strict-Transport-Security: max-age=31536000` when the forwarded proto is https, plus
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and
`Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` on JSON responses
(relaxed only on `/docs`).
- T10.1.a **[edge]** `openssl s_client -connect $HOST:443 -servername $HOST -tls1_1 </dev/null` fails to handshake; `-tls1_2` and `-tls1_3` succeed.
- T10.1.b **[edge]** `curl -sI http://$HOST/livez` → 301/308 to `https://`; `curl -sI https://$HOST/livez` has `strict-transport-security`.
- T10.1.c **[edge]** Certificate chain verifies with the system store: `curl --fail https://$HOST/livez` exits 0 without `-k`.
- T10.1.d **[box]** Security headers present on a JSON response; `/docs` still renders (HTML 200 with its script loaded).

**P10.2 Client address and proxy trust.**
- T10.2.a **[box]** With `FORWARDED_ALLOW_IPS=127.0.0.1`, a request carrying `X-Forwarded-For: 203.0.113.9` from 127.0.0.1 is rate-limited under key `203.0.113.9` (exposed in a test-only debug header or the access log).
- T10.2.b **[box]** The same header from a non-trusted source address is ignored (key = real peer address). Run with `FORWARDED_ALLOW_IPS=10.255.255.1` to simulate.

**P10.3 Connection reuse and timeouts.**
- T10.3.a **[box]** `curl -sv $RW_BASE/livez $RW_BASE/livez 2>&1 | grep -c 'Re-using existing connection'` → ≥ 1 (keep-alive works).
- T10.3.b **[box]** Idle connection survives 60 s: open a connection with Python `http.client`, request, sleep 60, request again on the same socket → 200 (keep-alive 75 s > typical 60 s proxy idle timeout).
- T10.3.c **[box]** Slowloris guard: a client that sends headers one byte per second is disconnected within `h11_max_incomplete_event_size`/timeout bounds (≤ 30 s) and other clients are unaffected (concurrent `/livez` stays < 50 ms).
- T10.3.d **[edge]** 200 sequential requests over one HTTP/2 connection (`curl --http2 -w '%{http_version}'`) report `2` and no connection resets.

**P10.4 Payload efficiency.**
`GZipMiddleware(minimum_size=1024)`.
- T10.4.a **[box]** `curl -s -H 'Accept-Encoding: gzip' -o /dev/null -w '%{size_download}'` vs without, on a 1000-row page: compressed ≤ 25 % of uncompressed. Record both numbers.
- T10.4.b **[box]** Responses < 1 KB are not compressed (no `content-encoding`).
- T10.4.c **[box]** `POST /v1/ingest-runs` with a 2 MB body → 413 problem, connection not held open.

**P10.5 CORS.**
Default: no CORS (server-to-server API). Optional `CORS_ALLOW_ORIGINS` list enables
`GET` only, no credentials.
- T10.5.a **[box]** With CORS unset, a preflight from `https://evil.test` gets no `access-control-allow-origin`.
- T10.5.b **[box]** With `CORS_ALLOW_ORIGINS=https://app.test`, preflight `GET` from that origin is allowed, `POST` is not.

**P10.6 Load and latency budget.**
`scripts/verify/load.py` (stdlib + httpx, already a dependency): open-loop at a fixed
rate, reports p50/p95/p99 and error counts as one JSON line.
- T10.6.a **[box][pg]** 100 rps for 120 s mixed (70 % rows, 20 % domains, 10 % search, within rate limits by using 10 tokens): p95 < 250 ms, p99 < 800 ms, 0 × 5xx.
- T10.6.b **[box][pg]** 3× the limit on one token: only 429s beyond the bucket, 0 × 5xx, and `/readyz` stays 200 throughout.
- T10.6.c **[edge]** 20 rps for 60 s against staging from outside the platform: p95 < 400 ms including TLS; record numbers.

Checklist
- [ ] T10.1.a TLS ≥ 1.2 only
- [ ] T10.1.b HTTP redirects to HTTPS, HSTS set
- [ ] T10.1.c certificate chain verifies
- [ ] T10.1.d security headers present, docs render
- [ ] T10.2.a trusted proxy address honoured
- [ ] T10.2.b untrusted forwarded header ignored
- [ ] T10.3.a keep-alive reuse
- [ ] T10.3.b idle connection survives 60 s
- [ ] T10.3.c slow clients cut off, others fine
- [ ] T10.3.d HTTP/2 at the edge, no resets
- [ ] T10.4.a gzip ratio recorded, ≤ 25 %
- [ ] T10.4.b small responses uncompressed
- [ ] T10.4.c oversized body refused
- [ ] T10.5.a no CORS by default
- [ ] T10.5.b CORS allowlist works for GET only
- [ ] T10.6.a local load within latency budget
- [ ] T10.6.b overload yields 429, never 5xx
- [ ] T10.6.c staging latency recorded
- [ ] GATE P10

---

### Phase 11. Scheduler migration (Supabase cron)

Depends on: P5 deployed to the environment the cron calls. This is the one step that
changes production behaviour; it ships in the same release as `/v1/ingest-runs` and
keeps the legacy route alive until it is verified.

**P11.1 New trigger function.**
Supabase migration `2026101000000y_schedule_v1.sql`: `trigger_ingest(slug, force)` posts
to `/v1/ingest-runs` with body `{"registers": [slug]}` (or `{}` for all) and
`Idempotency-Key: registerwatch-cron-<YYYY-MM-DD>-<HH>-<slug>`, so a pg_net retry or
a double-fire of the same slot creates one run. Signature unchanged.
- T11.1.a `tests/test_schema.py::test_cron_migration_targets_v1` parses the SQL and asserts the URL contains `/v1/ingest-runs`, an `Idempotency-Key` header is sent, and `/ingest/` (legacy) no longer appears.
- T11.1.b **[edge]** In the Supabase SQL editor: `select registerwatch_private.trigger_ingest('pl_mf');` then `select status_code from net._http_response order by created desc limit 1;` → `202`. Run it twice within the hour → the second returns `200` and `GET /v1/ingest-runs` shows one run.
- T11.1.c **[edge]** After the next 06:00 UTC slot: `select status, return_message from cron.job_run_details order by start_time desc limit 1` → `succeeded`, and `/v1/status` shows every register fresh.

Checklist
- [ ] T11.1.a cron SQL targets v1 with an idempotency key
- [ ] T11.1.b manual trigger creates exactly one run
- [ ] T11.1.c scheduled slot succeeded end to end
- [ ] GATE P11

---

### Phase 12. Legacy deprecation, removal, sign-off

Depends on: P11 verified in production.

**P12.1 Deprecation headers on legacy routes (release 0.3.0).**
- T12.1.a `tests/test_legacy.py`: every legacy route answers with `Deprecation`, `Sunset` (HTTP-date, 90 days after the release), and `Link: <v1 equivalent>; rel="successor-version"`.
- T12.1.b Legacy routes are marked `deprecated: true` in the spec.
- T12.1.c Access logs record `legacy_route=true`; `scripts/verify/legacy_usage.sh` counts legacy hits for the last 7 days from the platform logs. Record the count.

**P12.2 Documentation.**
README "API" section rewritten for v1, with the migration table from 2.2 and the error
catalog; `docs/problems.md` merged to `main`, so the type URIs resolve (2.5).
- T12.2.a Every `type` URI in the catalog returns 200 from github.com and its page has the anchor for the slug.
- T12.2.b `uv run pytest tests/test_docs.py`: every path in the README's API tables exists in the spec (no stale docs).

**P12.3 Removal (release 0.4.0, after Sunset and zero legacy hits for 14 days).**
- T12.3.a Legacy routes return 410 `gone` problem with the successor link for one more release, then are deleted; `breaking.sh` against 0.3.x reports only the documented legacy removals.
- T12.3.b `legacy_usage.sh` shows 0 hits for 14 consecutive days before the merge.

**P12.4 Final contract sign-off.**
- T12.4.a `npx @redocly/cli lint openapi/v1.yaml` → **0 errors, 0 warnings**.
- T12.4.b schemathesis over all v1 operations with 200 examples each → no failures, and `tests/contract_baseline.json` is `{}` for both modes.
- T12.4.c `uv run pytest -q` (with Postgres) and `uv run lint-imports` → green.
- T12.4.d `npx @stoplight/prism-cli mock openapi/v1.yaml` serves every operation, and `scripts/verify/load.py --base http://127.0.0.1:4010 --smoke` gets 2xx from each (the spec alone is enough to build a client against).
- T12.4.e Every finding F1–F18 in section 1.3 maps to a struck test ID; record the map in the evidence log.

Checklist
- [ ] T12.1.a deprecation headers on legacy routes
- [ ] T12.1.b legacy marked deprecated in spec
- [ ] T12.1.c legacy usage counted
- [ ] T12.2.a problem type URIs resolve
- [ ] T12.2.b README matches spec
- [ ] T12.3.a legacy routes gone with 410 then removed
- [ ] T12.3.b zero legacy hits for 14 days
- [ ] T12.4.a lint clean: 0 errors, 0 warnings
- [ ] T12.4.b schemathesis clean
- [ ] T12.4.c full suite green
- [ ] T12.4.d mock server serves the whole spec
- [ ] T12.4.e every finding mapped to a passing test
- [ ] GATE P12

---

## 4. Master checklist

Strike a phase here only after its `GATE` line is struck.

- [x] ~~Phase 1. Contract baseline and tooling~~
- [x] ~~Phase 2. Versioned routing, operation ids, typed responses~~
- [x] ~~Phase 3. Errors (RFC 9457) and input validation~~
- [x] ~~Phase 4. Stable pagination and a complete change feed~~
- [x] ~~Phase 5. Ingest runs as a persisted resource~~
- [ ] Phase 6. Security schemes and authorization
- [ ] Phase 7. Caching, rate limiting, query cost
- [ ] Phase 8. Probes, status, and the database pool
- [ ] Phase 9. OS and container level
- [ ] Phase 10. Network level
- [ ] Phase 11. Scheduler migration (Supabase cron)
- [ ] Phase 12. Legacy deprecation, removal, sign-off

## 5. Finding → test map

| Finding | Closed by |
|---|---|
| F1 unstable pagination | T4.1.c, T4.2.a, T4.2.b |
| F2 unbounded paging inputs | T3.2.a, T3.2.e |
| F18 NUL bytes are a 500 | T3.2.f, T3.2.e |
| F3 process-local ingest state | T5.2.a, T5.2.b, T5.3.f |
| F4 truncated change feed | T4.3.b |
| F5 no security schemes | T6.1.a, T6.2.a–c |
| F6 untyped responses | T2.3.a–c |
| F7 non-RFC 9457 errors | T3.1.a–d |
| F8 verbs / RPC shapes | T2.1.c, T5.3.a |
| F9 no versioning | T1.2.a, T12.1.a, T12.3.a |
| F10 filter namespace collision | T4.2.d |
| F11 redundant row path | T2.1.c |
| F12 no rate limit / cache / costly queries | T7.1.a, T7.2.a, T7.3.a–b |
| F13 overloaded 503 on status | T8.1.a–b |
| F14 pool starvation | T7.3.c, T8.2.a–c |
| F15 spec hygiene | T2.2.a, T2.4.a |
| F16 platform defaults | T9.1.a–T10.6.c |
| F17 timestamps | T3.2.b, T4.3.d |
