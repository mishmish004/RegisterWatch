"""The API's contract: the committed spec, and the app held to it.

openapi/v1.yaml is generated from the app, like the register DDL, and must stay
generated: clients are built from it and scripts/verify/breaking.sh diffs it.

Schemathesis then sends generated requests to every operation and checks each
response against the spec. It runs against real Postgres with every register's
fixture rows when REGISTERWATCH_TEST_DATABASE_URL is set ("pg"), and otherwise
against an empty fake connection that still runs the real query code ("fake").
The engine is always faked: nothing here fetches a regulator.

Failures today's API is known to have are pinned in contract_baseline.json, per
mode, operation and check. A failure not pinned there fails the suite, and so
does a pinned one that no longer happens, so the file only ever shrinks as the
plan's phases land. Regenerate it after a deliberate change with
REGISTERWATCH_CONTRACT_BASELINE=write.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
from types import SimpleNamespace

import psycopg
import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from psycopg.rows import dict_row
from schemathesis.checks import (
    content_type_conformance,
    not_a_server_error,
    response_schema_conformance,
    status_code_conformance,
)
from schemathesis.core.failures import Failure, FailureGroup

from registerwatch import api, jurisdictions
from registerwatch.cli import OPENAPI_SPEC, openapi_yaml
from registerwatch.db.repos import ingest_runs as ingest_repo
from registerwatch.http import deps
from registerwatch.ingest import runs
from registerwatch.ingest.engine import IngestResult
from registerwatch.registers import REGISTRY, all_registers
from tests.test_postgres import DSN, db, loaded  # noqa: F401 — fixtures, used in pg mode

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = pathlib.Path(__file__).with_name("contract_baseline.json")
MODE = "pg" if DSN else "fake"
TOKEN = "contract-test-token"
CHECKS = {c.__name__: c for c in (not_a_server_error, status_code_conformance,
                                  content_type_conformance, response_schema_conformance)}
EXAMPLES = 40  # per operation
SEED = 20261007


def test_spec_file_matches_app():
    assert (ROOT / OPENAPI_SPEC).read_text() == openapi_yaml(), \
        f"{OPENAPI_SPEC} is stale: run `uv run registerwatch openapi --write`"


# --- the v1 spec's own rules (plan.md Phase 2) ----------------------------------------

V1_TAGS = {"jurisdictions", "registers", "rows", "changes", "search", "domains", "ingest", "operations"}


def _operations(prefix: str = "") -> list[tuple[str, str, dict]]:
    spec = api.app.openapi()
    return [(m.upper(), p, op) for p, item in spec["paths"].items() if p.startswith(prefix)
            for m, op in item.items()]


# Unversioned paths, but v1 operations (plan.md 2.2), held to the same rules.
PROBES = {"/livez", "/readyz"}


def _v1_operations() -> list[tuple[str, str, dict]]:
    return [(m, p, op) for m, p, op in _operations() if p.startswith("/v1/") or p in PROBES]


def _resolve(schema: dict) -> dict:
    ref = schema.get("$ref", "")
    return api.app.openapi()["components"]["schemas"][ref.rsplit("/", 1)[1]] if ref else schema


def _ok_responses(op: dict):
    for status, resp in op["responses"].items():
        if status.startswith("2"):
            yield status, resp, resp.get("content", {}).get("application/json", {})


def test_no_authorization_header_param():
    """T6.1.b (F5): auth is a security scheme, not a string parameter. Every
    operation, legacy included, says how it authenticates; open ones say `[]`."""
    spec = api.app.openapi()
    schemes = spec["components"]["securitySchemes"]
    assert schemes.keys() == {"ReadToken", "IngestToken"}
    assert all(s["type"] == "http" and s["scheme"] == "bearer" for s in schemes.values())
    params = {f"{m} {p}": prm["name"] for m, p, op in _operations() for prm in op.get("parameters", [])
              if prm["in"] == "header" and prm["name"].lower() == "authorization"}
    assert not params
    read, ingest = [{"ReadToken": []}, {"IngestToken": []}], [{"IngestToken": []}]
    open_ = {"GET /health", "GET /registers", "GET /status", "GET /livez", "GET /readyz", "GET /v1/status"}
    for m, p, op in _operations():
        want = [] if f"{m} {p}" in open_ else ingest if "ingest" in p else read
        assert op.get("security") == want, f"{m} {p}"


def test_operation_ids_are_explicit():
    """v1 ids are chosen, not generated; legacy ids stay as they were for existing clients."""
    v1 = [op["operationId"] for _, _, op in _v1_operations()]
    every = [op["operationId"] for _, _, op in _operations()]
    assert v1 and not [i for i in v1 if "__" in i or "_" in i], v1
    assert len(every) == len(set(every))


def test_every_v1_operation_has_one_known_tag():
    bad = {f"{m} {p}": op.get("tags") for m, p, op in _v1_operations()
           if len(op.get("tags", [])) != 1 or op["tags"][0] not in V1_TAGS}
    assert not bad


def test_no_untyped_2xx_responses_in_v1():
    untyped = []
    for m, p, op in _v1_operations():
        for status, _, media in _ok_responses(op):
            schema = _resolve(media.get("schema", {}))
            if not schema.get("properties") and schema.get("type") in (None, "object"):
                untyped.append(f"{m} {p} {status}")
    assert not untyped


def test_every_v1_2xx_response_has_an_example():
    missing = []
    for m, p, op in _v1_operations():
        for status, _, media in _ok_responses(op):
            if not (media.get("example") or media.get("examples") or _resolve(media.get("schema", {})).get("examples")):
                missing.append(f"{m} {p} {status}")
    assert not missing


def test_schema_examples_validate_against_their_schemas():
    """FastAPI strips nulls from examples; http/openapi.py puts them back. Check it held."""
    from registerwatch.http import models

    for name, schema in api.app.openapi()["components"]["schemas"].items():
        model = getattr(models, name, None)
        for example in schema.get("examples", []):
            assert model is not None, name
            model.model_validate(example)


# --- error responses (plan.md Phase 3) ------------------------------------------------

# Path parameters that name a resource, so an unknown value is a 404. `domain` is a
# lookup key: any hostname has a status, possibly empty.
RESOURCE_IDS = {"code", "slug", "table", "id"}
# Ingest operations take the ingest token, so a read token is a 403; creating a
# run can also find its registers missing, a run going, a body over 64 KiB, a
# non-JSON body, or a reused Idempotency-Key.
INGEST_ERRORS = {"createIngestRun": {"403", "404", "409", "413", "415", "422"},
                 "listIngestRuns": {"403"}, "getIngestRun": {"403"}}
# Operations whose queries run under the read statement timeout and would rather
# fail than degrade: a cancelled one is a 504 (plan.md P7.3). The rest read no
# rows, or only freshness, which degrades to null.
QUERY_TIMEOUTS = {"listJurisdictionChanges", "listRows", "getRow", "listRegisterChanges", "listSnapshots",
                  "search", "getDomainStatus", *INGEST_ERRORS}
# The probes are never rate limited and take no parameters (a NUL byte is still
# a 400); only /readyz needs the database. Their whole list, then.
PROBE_ERRORS = {"livez": {"400", "500"}, "readyz": {"400", "500", "503"}}
# Not a problem: with `strict=true`, the status document itself, as a 503.
STATUS_DOCUMENT = ("getStatus", "503")


def test_error_responses_documented():
    """T3.1.c: what each v1 operation may answer besides success, by reference to
    the catalog's components.responses, and nothing FastAPI added on its own."""
    spec = api.app.openapi()
    wrong = {}
    for m, p, op in _v1_operations():
        op_id = op["operationId"]
        path_ids = {q["name"] for q in op.get("parameters", []) if q["in"] == "path"} & RESOURCE_IDS
        # 400 everywhere: an operation without parameters still refuses undeclared ones.
        # 401 where a token can be asked for; open operations (`security: []`) never ask.
        want = PROBE_ERRORS.get(op_id) or (
            {"400", "429", "500", "503"} | ({"401"} if op["security"] else set())
            | ({"404"} if path_ids else set()) | INGEST_ERRORS.get(op_id, set())
            | ({"504"} if op_id in QUERY_TIMEOUTS else set()))
        errors = {s for s in op["responses"] if s[0] in "45"}
        if errors != want:
            wrong[f"{m} {p}"] = sorted(errors ^ want)
        for s in errors:
            if (op_id, s) == STATUS_DOCUMENT:
                media = op["responses"][s]["content"]["application/json"]
                assert media["schema"] == {"$ref": "#/components/schemas/Status"}
                continue
            ref = op["responses"][s].get("$ref", "")
            assert ref.rsplit("/", 1)[-1] in spec["components"]["responses"], f"{m} {p} {s}: {ref!r}"
            assert ref.startswith("#/components/responses/")
    assert not wrong, f"missing or extra error responses: {wrong}"


def test_error_response_examples_are_problems():
    from registerwatch.http import models, problems

    for name, _, _ in problems.RESPONSES.values():
        media = api.app.openapi()["components"]["responses"][name]["content"][problems.MEDIA_TYPE]
        assert media["schema"] == {"$ref": "#/components/schemas/Problem"}, name
        models.Problem.model_validate(media["example"])
        assert media["example"]["type"].startswith(problems.DOCS + "#")


def test_no_v1_operation_is_pinned_in_the_baseline():
    """T3.2.e: generated traffic finds nothing wrong with v1, in either mode."""
    pinned = {op for mode in json.loads(BASELINE.read_text()).values() for op in mode}
    assert not [op for op in pinned if " /v1/" in op or op.split(" ", 1)[1] in PROBES]


# --- the app, wired for generated traffic ------------------------------------------

class _Result:
    """What the fake connection answers to anything: no rows, zero counts, and
    no row for a lookup of one (`SELECT * ... WHERE id = ...`)."""

    def __init__(self, one=True):
        self.one = one

    def fetchone(self):
        return {"n": 0, "id": None} if self.one else None

    def fetchall(self):
        return []


class _MemoryRuns:
    """ingest_runs without a database, for the fake mode."""

    def __init__(self):
        self.runs = {}

    def create(self, conn, run_id, requested, registers, key, request_hash):
        from datetime import datetime, timezone

        self.runs[run_id] = {"id": run_id, "status": "queued", "requested": requested, "registers": registers,
                             "not_started": [], "idempotency_key": key, "request_hash": request_hash,
                             "created_at": datetime.now(timezone.utc), "started_at": None, "finished_at": None,
                             "error": None, "results": []}
        return self.runs[run_id]

    def by_key(self, conn, key):
        return next((r for r in self.runs.values() if key and r["idempotency_key"] == key), None)

    def get(self, conn, run_id):
        return self.runs.get(run_id)

    def active(self, conn):
        return None

    def page(self, conn, *, before, limit):
        rows = sorted(self.runs.values(), key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return [r for r in rows if before is None or (r["created_at"], r["id"]) < before][:limit + 1]


class _Lease:
    def alive(self):
        return True

    def release(self):
        pass


class _FakeConn:
    def execute(self, query, *_a, **_k):
        text = query if isinstance(query, str) else query.as_string(None)
        return _Result(one=not (text.startswith("SELECT * FROM") and "WHERE id =" in text))


@pytest.fixture(scope="module")
def app_under_test(request):
    cfg = SimpleNamespace(ingest_token=TOKEN, read_token="", stale_after_h=26.0, log_level="WARNING")

    def fake_many(registers, store, *, force=False, accept_count_delta=False):
        return [IngestResult(r.slug, 7, True, None, 1, 1, record_count=10, raw_hash=b"\x01") for r in registers]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(api, "settings", lambda: cfg)
        mp.setattr(deps, "settings", lambda: cfg)
        mp.setattr(api, "make_store", lambda: object())
        mp.setattr(api.engine, "ingest_many", fake_many)
        tx = request.getfixturevalue("loaded") if MODE == "pg" else (lambda: contextlib.nullcontext(_FakeConn()))
        mp.setattr(api, "tx", tx)    # legacy routes
        mp.setattr(deps, "tx", tx)   # v1
        # v1 ingest runs: never a real fetch. In pg mode the runs and their lock are real.
        mp.setattr(runs, "make_store", lambda: object())
        mp.setattr(runs.engine, "ingest", lambda r, store, **kw: IngestResult(r.slug, None, True, None, 1, 1,
                                                                              record_count=10))
        if MODE == "pg":
            mp.setattr(runs, "tx", tx)
            mp.setattr(runs, "connect", lambda: psycopg.connect(DSN, autocommit=True, row_factory=dict_row))
        else:
            memory = _MemoryRuns()
            for name in ("create", "by_key", "get", "active", "page"):
                mp.setattr(ingest_repo, name, getattr(memory, name))
            mp.setattr(runs, "acquire", lambda: _Lease())
            mp.setattr(runs, "execute", lambda lease, run_id, targets, **kw: lease.release())
        api._last.clear()
        yield api.app
    if api._running.locked():
        api._running.release()


@pytest.fixture(scope="module")
def api_schema(app_under_test):
    schema = schemathesis.openapi.from_asgi("/openapi.json", app_under_test)
    # Same requests every run: the baseline is compared exactly, so generation
    # must not drift between runs (schemathesis otherwise seeds at random).
    schema.config.seed = SEED
    schema.config.generation.update(deterministic=True, max_examples=EXAMPLES)

    # Random path segments are nearly always 404s. Mix in real identifiers so
    # generation reaches the query code behind them, where the bugs are.
    codes = [c.lower() for c in jurisdictions.by_code()]
    tables = [(jurisdictions.normalise(r.country).lower(), r.slug, t.name)
              for r in all_registers() for t in r.tables]
    slug_tables = [{"slug": s, "table": t} for _, s, t in tables]
    real = {
        "code": st.sampled_from(codes),
        "slug": st.sampled_from([*REGISTRY, "all"]),
        "domain": st.sampled_from(["bet365.com", "www.betway.com", "nj.betmgm.com", "example.org"]),
    }

    @schema.hook
    def flatmap_path_parameters(ctx, path_parameters):
        keys = set(path_parameters or {})
        if {"code", "slug", "table"} <= keys:
            choice = st.sampled_from(tables).map(lambda t: dict(zip(("code", "slug", "table"), t)))
        elif keys == {"slug", "table"}:
            choice = st.sampled_from(slug_tables)
        elif keys and keys <= set(real):
            choice = st.fixed_dictionaries({k: real[k] for k in keys})
        else:
            return st.just(path_parameters)
        return st.one_of(st.just(path_parameters), choice)

    @schema.hook
    def flatmap_headers(ctx, headers):
        # Half the requests carry the real token, so ingest is reached as well as refused.
        return st.one_of(st.just(headers), st.just({**(headers or {}), "authorization": f"Bearer {TOKEN}"}))

    return schema


schema = schemathesis.pytest.from_fixture("api_schema")
FOUND: dict[str, dict[str, str]] = {}   # operation -> {check: one failing request, as curl}
RAN: set[str] = set()


def _pinned() -> dict[str, list[str]]:
    data = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    return data.get(MODE, {})


@schema.parametrize()
@settings(max_examples=EXAMPLES, derandomize=True, deadline=None, database=None,
          suppress_health_check=list(HealthCheck))
def test_operation_conforms_to_the_spec(case):
    op = case.operation.label
    RAN.add(op)
    pinned = set(_pinned().get(op, []))
    try:
        response = case.call()
    except Exception as exc:  # noqa: BLE001 — the test client re-raises what uvicorn would send as a 500
        _fail(op, "not_a_server_error", case, pinned, f"unhandled {type(exc).__name__}: {exc}")
        return
    for name, check in CHECKS.items():
        if name == "not_a_server_error" and _stale_by_request(case, response):
            continue
        try:
            case.validate_response(response, checks=[check])
        except (Failure, FailureGroup) as exc:
            titles = [f.title for f in getattr(exc, "exceptions", [exc])]
            _fail(op, name, case, pinned, "; ".join(titles) + f" (HTTP {response.status_code})\n{exc}")


def _stale_by_request(case, response) -> bool:
    """`/v1/status?strict=true` answering 503 because something is stale is the
    contract (T8.1), not a server error. The other checks still hold it to the
    spec: a documented 503 whose body is the status document. In fake mode no
    register has a snapshot, so every one is stale."""
    return (case.operation.label == "GET /v1/status" and response.status_code == 503
            and response.json().get("stale") is True)


def _fail(op: str, check: str, case, pinned: set[str], detail: str) -> None:
    FOUND.setdefault(op, {}).setdefault(check, f"{case.as_curl_command()}\n  {detail.splitlines()[0]}")
    if check not in pinned and os.environ.get("REGISTERWATCH_CONTRACT_BASELINE") != "write":
        raise AssertionError(f"{op}: new contract failure {check} (not in {BASELINE.name}[{MODE!r}])"
                             f"\n{case.as_curl_command()}\n{detail}")


def test_every_operation_was_exercised_and_the_baseline_is_current(api_schema):
    """Runs after the parametrized test above (file order). Holds the baseline to
    what generation found: a pinned failure that no longer occurs must be removed."""
    spec = api.app.openapi()
    ops = {f"{m.upper()} {p}" for p, item in spec["paths"].items() for m in item}
    if RAN != ops:
        pytest.skip(f"the conformance test did not run every operation ({len(RAN)}/{len(ops)}); "
                    "run the whole file")
    found = {op: sorted(checks) for op, checks in sorted(FOUND.items())}
    if os.environ.get("REGISTERWATCH_CONTRACT_BASELINE") == "write":
        data = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
        data[MODE] = found
        BASELINE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        for op, d in sorted(FOUND.items()):  # visible with -s: one failing request per pinned entry
            for name, curl in sorted(d.items()):
                print(f"{MODE} | {op} | {name}\n  {curl}")
        pytest.skip(f"wrote {BASELINE.name}[{MODE!r}]")
    fixed = {op: sorted(set(c) - set(found.get(op, []))) for op, c in _pinned().items()}
    fixed = {op: c for op, c in fixed.items() if c}
    assert not fixed, f"pinned failures no longer occur; remove them from {BASELINE.name}[{MODE!r}]: {fixed}"
