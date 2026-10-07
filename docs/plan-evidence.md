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
