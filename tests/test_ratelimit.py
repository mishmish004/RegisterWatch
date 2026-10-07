"""Phase 7, P7.2: a token bucket per client and class of request, in the
process, with the IETF draft's RateLimit headers on every limited response
(plan.md P7.2). The clock is the test's; the database is faked."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from registerwatch import api
from registerwatch.http import deps, problems, ratelimit
from registerwatch.http.problems import Catalog
from tests.test_caching import Clock
from tests.test_contract import _FakeConn

ROWS = "/v1/registers/gb_ukgc/tables/licences/rows"
SEARCH = "/v1/search?q=bet"
READ_TOKEN = "read-token-0123456789abcdefghijklmnopqrstuvwxyz"
INGEST_TOKEN = "ingest-token-0123456789abcdefghijklmnopqrstuvwxyz"
AS_READER = {"Authorization": f"Bearer {READ_TOKEN}"}
AS_INGEST = {"Authorization": f"Bearer {INGEST_TOKEN}"}


@pytest.fixture
def client(monkeypatch):
    cfg = SimpleNamespace(ingest_token=INGEST_TOKEN, read_token="", stale_after_h=26.0, log_level="WARNING",
                          rate_limit_read_per_min=600, rate_limit_search_per_min=60, rate_limit_ingest_per_min=10)
    for module in (deps, api, ratelimit):
        monkeypatch.setattr(module, "settings", lambda: cfg)
    monkeypatch.setattr(deps, "tx", lambda: contextlib.nullcontext(_FakeConn()))
    monkeypatch.setattr(api, "tx", lambda: contextlib.nullcontext(_FakeConn()))
    monkeypatch.setattr(deps.repo, "data_versions", lambda conn: [{"slug": "gb_ukgc", "complete": 7, "latest": 7}])
    clock = Clock()
    monkeypatch.setattr(ratelimit.BUCKETS, "clock", clock)
    with TestClient(api.app) as c:
        c.cfg, c.clock = cfg, clock
        yield c


def _left(r) -> int:
    """`r` of the RateLimit field."""
    return int(r.headers["ratelimit"].split(";r=")[1].split(";")[0])


# --- T7.2.a ------------------------------------------------------------------------------

def test_the_61st_search_in_a_burst_is_a_429_with_retry_after(client):  # T7.2.a
    answers = [client.get(SEARCH) for _ in range(61)]
    assert [r.status_code for r in answers[:60]] == [200] * 60
    refused = answers[60]
    assert refused.status_code == 429
    assert refused.headers["content-type"] == "application/problem+json"
    body = refused.json()
    assert body["type"] == Catalog.RATE_LIMITED.type and body["status"] == 429
    assert body["detail"] == "60 requests per minute for searches and domain checks; retry in 1 s"
    assert body["request_id"] == refused.headers["x-request-id"]
    assert int(refused.headers["retry-after"]) >= 1
    assert refused.headers["ratelimit"] == '"search";r=0;t=1'
    # The bucket refills at the quota's rate: one search a second, no sooner.
    client.clock.t += 0.5
    assert client.get(SEARCH).status_code == 429
    client.clock.t += 0.5
    assert client.get(SEARCH).status_code == 200
    assert client.get(SEARCH).status_code == 429
    # A minute later the whole burst is back, and never more than that.
    client.clock.t += 3600
    assert _left(client.get(SEARCH)) == 59


def test_domains_and_legacy_searches_share_the_search_bucket(client):
    for path in ["/v1/domains/bet365.com", "/search?q=bet", "/check/domain/bet365.com"] * 20:
        assert client.get(path).status_code == 200
    legacy = client.get("/search?q=bet")
    assert legacy.status_code == 429 and legacy.headers["content-type"] == "application/json"
    assert legacy.json() == {"detail": "60 requests per minute for searches and domain checks; retry in 1 s"}
    assert int(legacy.headers["retry-after"]) >= 1
    # Reads are a bucket of their own.
    assert client.get(ROWS).status_code == 200


# --- T7.2.b ------------------------------------------------------------------------------

def test_the_headers_count_down_whatever_the_answer(client):  # T7.2.b
    first = client.get(ROWS)
    answers = [
        first,
        client.get(ROWS, headers={"If-None-Match": first.headers["etag"]}),  # 304
        client.get(ROWS + "?limit=0"),                                      # 400
        client.get("/v1/registers/xx_nope"),                                # 404
        client.get("/v1/ingest-runs"),                                      # 401
        client.get("/jurisdictions"),                                        # legacy
        client.get("/openapi.json"),
    ]
    assert [r.status_code for r in answers] == [200, 304, 400, 404, 401, 200, 200]
    assert [_left(r) for r in answers] == list(range(599, 592, -1))
    for r in answers:
        assert r.headers["ratelimit-policy"] == '"read";q=600;w=60'
        assert r.headers["ratelimit"].endswith(";t=1")  # 10 reads a second come back: the next one within 1 s


def test_t_says_when_the_next_request_is_back(client):
    client.cfg.rate_limit_search_per_min = 30  # one every 2 s
    assert client.get(SEARCH).headers["ratelimit"] == '"search";r=29;t=2'
    client.clock.t += 1
    assert client.get(SEARCH).headers["ratelimit"] == '"search";r=28;t=1'  # 28.5 left: the 29th is 1 s away
    client.clock.t += 1
    assert client.get(SEARCH).headers["ratelimit"] == '"search";r=28;t=2'
    client.clock.t += 600
    assert client.get(SEARCH).headers["ratelimit"] == '"search";r=29;t=2'


def test_t_is_zero_for_a_full_bucket(client):
    assert ratelimit.headers("read", 600, 600.0)["RateLimit"] == '"read";r=600;t=0'


def test_a_quota_of_zero_switches_that_limit_off(client):
    client.cfg.rate_limit_search_per_min = 0
    answers = [client.get(SEARCH) for _ in range(100)]
    assert {r.status_code for r in answers} == {200}
    assert not any("ratelimit" in r.headers for r in answers)
    assert _left(client.get(ROWS)) == 599


# --- T7.2.c ------------------------------------------------------------------------------

def test_each_token_has_its_own_bucket_and_others_count_as_their_address(client):  # T7.2.c
    client.cfg.read_token = READ_TOKEN
    for _ in range(60):
        assert client.get(SEARCH, headers=AS_READER).status_code == 200
    assert client.get(SEARCH, headers=AS_READER).status_code == 429
    assert _left(client.get(SEARCH, headers=AS_INGEST)) == 59  # the ingest token reads too, on its own count
    # Without a token, or with one this deployment did not issue, a client is its address.
    assert client.get(SEARCH).status_code == 401
    unknown = client.get(SEARCH, headers={"Authorization": "Bearer made-up-0123456789abcdefghijklmnopqrstuv"})
    assert unknown.status_code == 401 and _left(unknown) == 58
    assert _left(client.get(SEARCH, headers={"Authorization": "Bearer another-made-up-one"})) == 57


def test_a_forwarded_for_header_does_not_make_a_new_client(client):
    """Only uvicorn's proxy-headers handling (for FORWARDED_ALLOW_IPS) may change the
    client address; the header alone buys no fresh bucket."""
    left = [_left(client.get(SEARCH, headers={"X-Forwarded-For": f"203.0.113.{i}"})) for i in range(3)]
    assert left == [59, 58, 57]


def test_clients_are_keyed_by_token_hash_or_address():
    client = ratelimit.client_of({"headers": [(b"authorization", f"Bearer {INGEST_TOKEN}".encode())],
                                  "client": ("198.51.100.7", 1234)})
    anon = ratelimit.client_of({"headers": [], "client": ("198.51.100.7", 1234)})
    assert anon == "ip:198.51.100.7"
    # The bearer is not ours under the hermetic settings (no tokens): the address.
    assert client == anon


def test_the_bucket_key_never_holds_a_token(client):
    client.get(SEARCH, headers=AS_INGEST)
    keys = list(ratelimit.BUCKETS._buckets)
    assert keys and not any(INGEST_TOKEN in k for _, k in keys)
    assert ("search", f"token:{ratelimit._digest(INGEST_TOKEN)[:16]}") in keys


def test_starting_ingest_runs_draws_on_the_ingest_bucket(client):
    client.cfg.ingest_token = ""  # ingest disabled: every start is a 503, which still counts
    starts = [client.post("/v1/ingest-runs", headers=AS_INGEST, json={"registers": ["pl_mf"]}) for _ in range(11)]
    assert [r.status_code for r in starts] == [503] * 10 + [429]
    assert starts[0].headers["ratelimit-policy"] == '"ingest";q=10;w=60'
    assert client.post("/ingest/pl_mf").status_code == 429  # legacy starts share it
    # Watching runs is a read.
    assert client.get("/v1/ingest-runs", headers=AS_INGEST).headers["ratelimit-policy"] == '"read";q=600;w=60'


@pytest.mark.parametrize("method, path, policy", [
    ("GET", "/v1/search", "search"),
    ("GET", "/v1/domains/x.com", "search"),
    ("GET", "/search", "search"),
    ("GET", "/check/domain/x.com", "search"),
    ("POST", "/v1/ingest-runs", "ingest"),
    ("POST", "/ingest/all", "ingest"),
    ("POST", "/jurisdictions/ch/ingest", "ingest"),
    ("GET", "/v1/ingest-runs", "read"),
    ("GET", "/v1/ingest-runs/0199c1a8-7c3e-7a52-9d1e-5b6f0c2a4e11", "read"),
    ("GET", "/ingest/last", "read"),
    ("GET", "/v1/registers", "read"),
    ("GET", "/v1/searches", "read"),
    ("GET", "/status", "read"),
    ("GET", "/health", None),
    ("GET", "/livez", None),
    ("GET", "/readyz", None),
])
def test_policy_of(method, path, policy):
    assert ratelimit.policy_of(method, path) == policy


def test_the_oldest_bucket_goes_first_when_full():
    clock = Clock()
    buckets = ratelimit.Buckets(clock, size=2)
    assert buckets.take("read", "a", 2) == (True, 1.0)
    assert buckets.take("read", "b", 2) == (True, 1.0)
    assert buckets.take("read", "a", 2) == (True, 0.0)  # a is now the newest
    assert buckets.take("read", "c", 2) == (True, 1.0)  # b goes
    assert buckets.take("read", "a", 2) == (False, 0.0)
    assert buckets.take("read", "b", 2) == (True, 1.0)  # forgotten: a full bucket again


# --- T7.2.d ------------------------------------------------------------------------------

def test_probes_are_never_limited(client):  # T7.2.d
    client.cfg.rate_limit_read_per_min = client.cfg.rate_limit_search_per_min = 1
    health = [client.get("/health") for _ in range(1000)]
    assert {r.status_code for r in health} == {200}
    assert not any("ratelimit" in r.headers for r in health)
    for path in ("/livez", "/readyz"):
        answers = [client.get(path) for _ in range(50)]
        assert {r.status_code for r in answers} == {200} and not any("ratelimit" in r.headers for r in answers)
    assert client.get(ROWS).status_code == 200 and client.get(ROWS).status_code == 429


# --- the spec ----------------------------------------------------------------------------

def test_the_spec_documents_the_headers_on_every_v1_response():
    spec = api.app.openapi()
    assert {"RateLimit-Policy", "RateLimit"} <= set(spec["components"]["headers"])
    for name, response in spec["components"]["responses"].items():
        assert {"RateLimit-Policy", "RateLimit"} <= set(response["headers"]), name
    for path, item in spec["paths"].items():
        for op in item.values():
            if not path.startswith("/v1/"):
                continue
            assert "429" in op["responses"], op["operationId"]
            for status, response in op["responses"].items():
                if status.startswith("2"):
                    assert {"RateLimit-Policy", "RateLimit"} <= set(response["headers"]), (op["operationId"], status)
    limited = problems.RESPONSES[429]
    assert Catalog.RATE_LIMITED in limited[2]
