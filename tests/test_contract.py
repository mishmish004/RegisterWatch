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

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from schemathesis.checks import (
    content_type_conformance,
    not_a_server_error,
    response_schema_conformance,
    status_code_conformance,
)
from schemathesis.core.failures import Failure, FailureGroup

from registerwatch import api, jurisdictions
from registerwatch.cli import OPENAPI_SPEC, openapi_yaml
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


# --- the app, wired for generated traffic ------------------------------------------

class _Result:
    """What the fake connection answers to anything: no rows, zero counts."""

    def fetchone(self):
        return {"n": 0, "id": None}

    def fetchall(self):
        return []


class _FakeConn:
    def execute(self, *_a, **_k):
        return _Result()


@pytest.fixture(scope="module")
def app_under_test(request):
    cfg = SimpleNamespace(ingest_token=TOKEN, read_token="", stale_after_h=26.0, log_level="WARNING")

    def fake_many(registers, store, *, force=False, accept_count_delta=False):
        return [IngestResult(r.slug, 7, True, None, 1, 1, record_count=10, raw_hash=b"\x01") for r in registers]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(api, "settings", lambda: cfg)
        mp.setattr(api, "make_store", lambda: object())
        mp.setattr(api.engine, "ingest_many", fake_many)
        if MODE == "pg":
            mp.setattr(api, "tx", request.getfixturevalue("loaded"))
        else:
            mp.setattr(api, "tx", lambda: contextlib.nullcontext(_FakeConn()))
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
        try:
            case.validate_response(response, checks=[check])
        except (Failure, FailureGroup) as exc:
            titles = [f.title for f in getattr(exc, "exceptions", [exc])]
            _fail(op, name, case, pinned, "; ".join(titles) + f" (HTTP {response.status_code})\n{exc}")


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
