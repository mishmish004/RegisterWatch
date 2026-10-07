"""Phase 11: the Supabase schedule starts v1 ingest runs.

T11.1.a (tests/test_schema.py) reads the migration. These run it: the function
Supabase ends up with, on plain Postgres, with `net.http_post` and Vault stood
in by a table each, so what it would send is a row. Then that request goes to
the app as sent, so the SQL and the API are held to each other.
scripts/verify/cron.sh runs it on Supabase's own Postgres image, pg_cron and
pg_net included.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.test_ingest_runs import INGEST_TOKEN, cfg, pg, pg_app  # noqa: F401 — fixtures
from tests.test_postgres import DSN, db, loaded  # noqa: F401 — fixtures
from tests.test_schema import SUPABASE, trigger_ingest

# pg_net's own signature (0.20, as on Supabase): the function calls it by these names.
STAND_INS = """
CREATE SCHEMA IF NOT EXISTS registerwatch_private;  -- the register schemas make it too
CREATE SCHEMA vault;
CREATE TABLE vault.decrypted_secrets (name text, decrypted_secret text);
CREATE SCHEMA net;
CREATE TABLE net.sent (id bigserial, url text, body jsonb, params jsonb, headers jsonb, timeout_milliseconds int);
CREATE FUNCTION net.http_post(url text, body jsonb DEFAULT '{}', params jsonb DEFAULT '{}',
                              headers jsonb DEFAULT '{"Content-Type": "application/json"}',
                              timeout_milliseconds int DEFAULT 5000) RETURNS bigint
  LANGUAGE sql AS $$ INSERT INTO net.sent (url, body, params, headers, timeout_milliseconds)
                     VALUES (url, body, params, headers, timeout_milliseconds) RETURNING id $$;
"""


def sent(calls: list[str], secrets: bool = True) -> tuple[list[dict], str]:
    """What trigger_ingest posts for each call (`select <call>`), and the UTC hour
    the calls ran in. Everything is rolled back."""
    import psycopg
    from psycopg.rows import dict_row

    name, *_ = trigger_ingest()
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        try:
            for role in ("anon", "authenticated"):  # Supabase's; the migration revokes from them
                if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                    c.execute(f"CREATE ROLE {role} NOLOGIN")
            c.execute(STAND_INS)
            if secrets:
                c.execute("INSERT INTO vault.decrypted_secrets VALUES ('registerwatch_api_url', 'https://api.example/'),"
                          " ('registerwatch_ingest_token', %s)", (INGEST_TOKEN,))
            c.execute((SUPABASE / name).read_text())
            ids = [c.execute(f"SELECT {call} AS id").fetchone()["id"] for call in calls]
            hour = c.execute("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD-HH24') AS h").fetchone()["h"]
            rows = {r["id"]: r for r in c.execute("SELECT * FROM net.sent")}
            return [rows[i] for i in ids], hour
        finally:
            c.rollback()


@pg
def test_the_schedule_posts_v1_runs_with_a_key_per_slot(loaded):  # noqa: F811
    calls = ["registerwatch_private.trigger_ingest()", "registerwatch_private.trigger_ingest('pl_mf')",
             "registerwatch_private.trigger_ingest('pl_mf', true)", "registerwatch_private.trigger_ingest('all', true)"]
    posts, hour = sent(calls)
    assert [(p["url"], p["body"], p["headers"]["Idempotency-Key"]) for p in posts] == [
        ("https://api.example/v1/ingest-runs", {}, f"registerwatch-cron-{hour}-all"),
        ("https://api.example/v1/ingest-runs", {"registers": ["pl_mf"]}, f"registerwatch-cron-{hour}-pl_mf"),
        ("https://api.example/v1/ingest-runs", {"registers": ["pl_mf"], "force": True},
         f"registerwatch-cron-{hour}-pl_mf-force"),
        ("https://api.example/v1/ingest-runs", {"force": True}, f"registerwatch-cron-{hour}-all-force"),
    ]
    assert re.fullmatch(r"\d{4}-\d\d-\d\d-\d\d", hour)
    for p in posts:
        assert p["headers"]["Authorization"] == f"Bearer {INGEST_TOKEN}"
        assert p["headers"]["Content-Type"] == "application/json"
        assert p["params"] == {} and p["timeout_milliseconds"] == 30000


@pg
def test_without_its_vault_secrets_the_schedule_fails_loudly(loaded):  # noqa: F811
    """A raised error is a failed run in cron.job_run_details, where a human looks."""
    import psycopg

    with pytest.raises(psycopg.errors.RaiseException, match="Vault secrets"):
        sent(["registerwatch_private.trigger_ingest()"], secrets=False)


@pg
def test_what_the_schedule_sends_starts_one_run_per_slot(pg_app):  # noqa: F811
    """The posts above, sent to the app as they are: a slot that fires twice gets
    its run back (200), a forced call in the same hour is a run of its own, not a
    422, and every register is in the `all` run."""
    posts, _ = sent(["registerwatch_private.trigger_ingest('pl_mf')", "registerwatch_private.trigger_ingest('pl_mf')",
                     "registerwatch_private.trigger_ingest('pl_mf', true)", "registerwatch_private.trigger_ingest()"])
    with pg_app.client() as c:
        answers = [c.post(p["url"].removeprefix("https://api.example"), content=json.dumps(p["body"]),
                          headers=p["headers"]) for p in posts]
    first, again, forced, every = answers
    assert [a.status_code for a in answers] == [202, 200, 202, 202], [a.text for a in answers]
    assert again.json()["id"] == first.json()["id"] != forced.json()["id"]
    assert forced.json()["requested"]["force"] is True
    assert len(every.json()["registers"]) == 21
    with pg_app.db() as conn:
        keys = [r["idempotency_key"] for r in conn.execute("SELECT idempotency_key FROM ingest_runs ORDER BY created_at")]
    assert keys == [p["headers"]["Idempotency-Key"] for p in (posts[0], posts[2], posts[3])]
