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
