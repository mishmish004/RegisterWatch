# Plan evidence log

One line per passed test in [plan.md](../plan.md): `test | date | commit | command | result`.
`[pg]` runs used `REGISTERWATCH_TEST_DATABASE_URL=postgresql://postgres:rw@localhost:55432/rw`
(`postgres:16-alpine` in Docker).

## Phase 1. Contract baseline and tooling

T1.1.a | 2026-10-07 | fc2fcfe | `registerwatch openapi --write && git diff --exit-code openapi/` | exit 0; committed openapi/v1.yaml (524 lines, 12 operations) matches the app
T1.1.b | 2026-10-07 | fc2fcfe | `uv run pytest tests/test_contract.py::test_spec_file_matches_app` | 1 passed
T1.1.c | 2026-10-07 | fc2fcfe | `npx @redocly/cli@2.59.0 lint v1.yaml` (no config) and `scripts/verify/lint.sh` | built-in recommended: 13 errors, 4 warnings (security-defined 12, no-empty-servers 1; operation-4xx-response 3, info-license 1), same as the review; repo config: 17 errors, 0 warnings (the 4 warnings raised to errors). Plan text updated: later phases count against lint.sh
T1.2.a | 2026-10-07 | fc2fcfe | remove required `msg` from `ValidationError` in the working tree; `scripts/verify/breaking.sh HEAD` | exit 1; "9 changes: 9 error", `response-required-property-removed` ... `detail/items/msg` on every operation using the 422 schema
T1.2.b | 2026-10-07 | fc2fcfe | add optional query param `lang` to `/search` in the working tree; `scripts/verify/breaking.sh HEAD` | exit 0; "No breaking changes to report"
T1.3.a | 2026-10-07 | fc2fcfe | `uv run pytest tests/test_contract.py`, fake and [pg] | 3 passed, 12 subtests (one per operation) in each mode; last test asserts operations run == spec's 12 and the baseline is current
T1.3.b | 2026-10-07 | fc2fcfe | [pg] `REGISTERWATCH_CONTRACT_BASELINE=write pytest -s tests/test_contract.py` | negative offset is a 500: `GET /jurisdictions/ch/ch_gespa/blocked_domains?offset=-1207` -> `InvalidRowCountInResultOffsetClause: OFFSET must not be negative`. Also found: `GET /jurisdictions/sk/changes?limit=-5629` -> `LIMIT must not be negative` (F2); `GET /search?q=%C3%9B%00...` -> `DataError: text fields cannot contain NUL` (new finding F18, added to plan as T3.2.f). Baseline file unchanged on rerun
GATE P1 | 2026-10-07 | fc2fcfe | `uv run pytest -q` (fake), same with [pg], `uv run lint-imports` | 131 passed, 9 skipped; [pg] 140 passed, 0 skipped; import contracts 1 kept, 0 broken

## Phase 2. Versioned routing, operation ids, typed responses

T2.1.a | 2026-10-07 | 590254f | `pytest tests/test_api.py`; `git diff 1216018 -- tests/test_api.py`; `scripts/verify/breaking.sh 1216018`; YAML compare of legacy paths | 18 passed; test file unchanged since Phase 1; no breaking change; the 12 legacy paths and the 2 schemas they use are identical to Phase 1's spec
T2.1.b | 2026-10-07 | 590254f | `uv run lint-imports` | 2 kept, 0 broken ("Register parsers are pure", "v1 does not depend on the legacy API"). Scratch import of `registerwatch.api` into `http/v1/search.py` broke the new contract (`registerwatch.http.v1.search -> registerwatch.api`), then reverted
T2.1.c | 2026-10-07 | 590254f | `pytest tests/test_api_v1.py`, plus the v1 routes against [pg] fixture rows | 13 passed. Against Postgres: `/v1/jurisdictions` 20 in one page, `/v1/registers` 21, `/v1/registers/gb_ukgc` with freshness, `getTable` with 7 typed columns; `ca_kgc.operators` walked by cursor at limit 37: 229 rows in 7 pages, equal to `include_total`
T2.2.a | 2026-10-07 | 590254f | `pytest tests/test_contract.py::test_operation_ids_are_explicit` | passed: 8 v1 ids, none with `_`, all 20 ids unique
T2.2.b | 2026-10-07 | 590254f | `pytest tests/test_contract.py::test_every_v1_operation_has_one_known_tag` | passed
T2.3.a | 2026-10-07 | 590254f | `pytest tests/test_contract.py::test_no_untyped_2xx_responses_in_v1` | passed
T2.3.b | 2026-10-07 | 590254f | `pytest tests/test_contract.py::test_every_v1_2xx_response_has_an_example` and `::test_schema_examples_validate_against_their_schemas` | both passed; Redocly reports 0 `no-invalid-schema-examples` (12 before null-restoring in http/openapi.py)
T2.3.c | 2026-10-07 | 590254f | `pytest tests/test_contract.py`, fake and [pg]; mutation script | 8 passed, 20 subtests, in both modes, with no new baseline entries; with `Row.values` changed to `integer` in a copy of the spec, a real listRows response fails `response_schema_conformance` ("Response violates schema")
T2.4.a | 2026-10-07 | 590254f | `scripts/verify/lint.sh` | `no-empty-servers` and `info-license` gone, `info-license-strict` not raised. 23 errors remain, all scheduled: `security-defined` ×20 (Phase 6) and `operation-4xx-response` ×3 on legacy /health, /registers, /status (Phases 3 and 8)
GATE P2 | 2026-10-07 | 590254f | `uv run pytest -q` (fake), same with [pg], `uv run lint-imports`, `openapi --write` + diff | 149 passed, 9 skipped; [pg] 158 passed; 2 contracts kept; spec current
Note | 2026-10-07 | 590254f | [pg] `GET /v1/search?q=a%00b`, `GET /v1/registers/gb_ukgc/tables/licences/rows?q=a%00b` | both 500: F18 applies to v1 too. Fixed in Phase 3 (T3.2.f), not here

## Phase 3. Errors (RFC 9457) and input validation

T3.1.a | 2026-10-07 | 37de3a6 | `pytest tests/test_errors.py::test_every_catalog_entry_is_a_problem`, fake and [pg] | 20 passed in each mode, one per catalog entry: status, `application/problem+json`, `type` = catalog URI, `instance` = path, `request_id` = echoed `X-Request-Id`, `WWW-Authenticate` on 401, `Retry-After: 5` on 503/504. 12 entries have their real trigger; 8 (row-not-found, ingest-*, unsupported-media-type, idempotency-key-reused, forbidden, rate-limited) are raised through `getRegister` by a patch until P4–P7. `test_every_problem_type_has_a_section_in_the_docs` passed
T3.1.b | 2026-10-07 | 37de3a6 | `pytest tests/test_errors.py::test_an_unhandled_error_leaks_nothing_and_is_logged_with_its_request_id` | passed: 500 `internal`, no `Traceback`, `.py` or exception text in the body, `X-Request-Id` echoed, one log record with that `request_id` and the traceback. With `UnhandledErrorMiddleware` removed: 3 tests fail
T3.1.c | 2026-10-07 | 37de3a6 | `pytest tests/test_contract.py::test_error_responses_documented ::test_error_response_examples_are_problems` | both passed: all 8 v1 operations document 400, 401, 429, 500, 503 as `$ref`s to components.responses, the 4 with resource ids also 404, and no 422
T3.1.d | 2026-10-07 | 37de3a6 | `scripts/verify/lint.sh` | `operation-4xx-response`: 0 (was 3; legacy /health, /registers, /status document the NUL-guard 400), no ignore file. 20 errors remain, all `security-defined` (Phase 6)
T3.2.a | 2026-10-07 | 37de3a6 | `pytest tests/test_errors.py::test_limit_is_bounded` | 4 passed: `limit=0`, `1001`, `-1`, `ten` → 400 `invalid-parameter`, `errors[0]` = `{field: limit, location: query}`
T3.2.b | 2026-10-07 | 37de3a6 | `pytest tests/test_errors.py::test_timestamps_must_carry_an_offset` | passed: `deps.Timestamp` accepts `Z` and `+02:00`, refuses `2026-10-01T00:00:00` ("timezone"). Route-level check is T4.3.d
T3.2.c | 2026-10-07 | 37de3a6 | `pytest tests/test_errors.py::test_domain_is_validated` | 3 passed: `not_a_host!!`, a 304-char name, `nodot` → 400 naming `domain` in `path`
T3.2.d | 2026-10-07 | 37de3a6 | `pytest tests/test_errors.py::test_search_q_length_is_validated` | 2 passed: `q=a` and 201 chars → 400 naming `q`
T3.2.e | 2026-10-07 | 37de3a6 | `REGISTERWATCH_CONTRACT_BASELINE=write pytest tests/test_contract.py`, fake and [pg]; then both without it | baseline lost its 3 [pg] `not_a_server_error` entries (legacy changes, rows, search: negative/out-of-range limit/offset, NUL); no v1 operation pinned in either mode (`test_no_v1_operation_is_pinned_in_the_baseline` passed). Left: fake `/status` 503 (F13, Phase 8) and legacy `status_code_conformance`
T3.2.f | 2026-10-07 | 37de3a6 | [pg] `pytest tests/test_errors.py::test_nul_bytes_are_a_400_against_postgres` | 5 passed: v1 search `q`, rows `filter[status]` and `q`, legacy search and rows → 400 with NUL, 200 without. With `NulGuardMiddleware` removed: 5 failed, `assert 500 == 400`
T3.3.a | 2026-10-07 | 37de3a6 | `pytest tests/test_errors.py::test_a_request_without_an_id_gets_a_uuid7` | passed: version 7, RFC 4122 variant, first 48 bits = request time in ms, unique per request, legacy routes too
T3.3.b | 2026-10-07 | 37de3a6 | `pytest tests/test_errors.py::test_a_supplied_id_is_echoed_and_an_unsafe_one_replaced ::test_log_records_carry_the_request_id` | both passed: `abc-123` echoed; 1 KB, space, tab, empty → new UUIDv7; a log record inside a request has its id, outside it `-`. With `RequestIdMiddleware` removed: 25 tests fail
Wire | 2026-10-07 | 37de3a6 | `uvicorn registerwatch.api:app` on 127.0.0.1 against [pg], `curl -D -` | `?limit=0` → `HTTP/1.1 400`, `content-type: application/problem+json`, `x-request-id` a UUIDv7 equal to the body's `request_id`; `X-Request-Id: wire-check-1` echoed on a 404; a 1 KB id replaced; `%00` on legacy `/search` and v1 rows → 400; `POST /v1/registers` → 405 with `allow: GET`
Breaking | 2026-10-07 | 37de3a6 | `scripts/verify/breaking.sh` and `scripts/verify/breaking.sh e7bc2b4` | against origin/main: PASS (no spec there yet). Against Phase 2: 5 expected narrowings, recorded in plan.md P3.2: legacy changes `limit` and rows `offset` min 0 / max bigint, v1 `domain` maxLength 253
GATE P3 | 2026-10-07 | 37de3a6 | `uv run pytest -q` (fake), same with [pg], `uv run lint-imports`, spec current | 191 passed, 14 skipped; [pg] 205 passed; 2 contracts kept; `openapi/v1.yaml` matches the app
