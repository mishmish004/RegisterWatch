"""The committed migrations: the register DDL is generated and must stay generated,
and the Supabase schedule (P11.1) starts v1 ingest runs."""

from __future__ import annotations

import pathlib
import re

import pytest

from registerwatch.cli import MIGRATIONS, SCHEMA_MIGRATION
from registerwatch.db.schema import all_ddl, register_ddl
from registerwatch.registers import REGISTRY, all_registers
from registerwatch.registers.base import DOMAIN_COLUMNS

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", [ROOT / "src" / "registerwatch" / "migrations" / SCHEMA_MIGRATION,
                                  ROOT / "supabase" / "migrations" / SCHEMA_MIGRATION])
def test_committed_ddl_matches_the_register_modules(path):
    assert path.read_text() == all_ddl(all_registers()), \
        f"{path.name} is stale: run `uv run registerwatch ddl --write`"


def test_one_schema_per_register_named_after_its_slug():
    for slug, reg in REGISTRY.items():
        sql = register_ddl(reg)
        assert f"CREATE SCHEMA IF NOT EXISTS {slug};" in sql
        for t in reg.tables:
            assert f"CREATE TABLE IF NOT EXISTS {slug}.{t.name} (" in sql
            assert f"CREATE OR REPLACE VIEW {slug}.current_{t.name} AS" in sql


def test_migrations_ship_inside_the_package():
    names = sorted(p.name for p in MIGRATIONS.iterdir() if p.name.endswith(".sql"))
    assert names[0] == "001_init.sql" and SCHEMA_MIGRATION in names
    # Supabase's CLI reads its own directory; the app-schema files must match it.
    for n in names:
        if n != "001_init.sql":
            assert (ROOT / "supabase" / "migrations" / n).read_text() == (MIGRATIONS / n).read_text(), n


def test_lookup_indexes_cover_every_searched_and_host_column():  # P7.3
    for slug, reg in REGISTRY.items():
        sql = register_ddl(reg)
        names = re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", sql)
        # Postgres truncates a longer name, which would make IF NOT EXISTS skip a different index.
        assert all(len(n) <= 63 for n in names) and len(names) == len(set(names)), slug
        for t in reg.tables:
            for c in t.columns:
                assert (f"{t.name}_{c.name}_trgm" in names) == c.searched, (slug, t.name, c.name)
                hostname = c.name in DOMAIN_COLUMNS and c.type == "text"
                assert {f"{t.name}_{c.name}_lower", f"{t.name}_{c.name}_reverse"} <= set(names) or not hostname


# --- P11.1 the Supabase schedule (Supabase-only: pg_cron, pg_net, Vault) ----------------

SUPABASE = ROOT / "supabase" / "migrations"
TRIGGER = re.compile(r"create or replace function registerwatch_private\.trigger_ingest\((.*?)\)\s*"
                     r"returns (\w+).*?as \$\$(.*?)\$\$;", re.DOTALL | re.IGNORECASE)


def trigger_ingest() -> tuple[str, str, str, str]:
    """The trigger_ingest Supabase ends up with: the last migration that defines
    it, in the order `supabase db push` applies them. (file, arguments, return
    type, body without comments)"""
    found = [(p.name, *m.groups()) for p in sorted(SUPABASE.glob("*.sql"))
             for m in TRIGGER.finditer(p.read_text())]
    assert found, "no migration defines registerwatch_private.trigger_ingest"
    name, args, returns, body = found[-1]
    return name, args, returns, re.sub(r"--[^\n]*", "", body)


def test_cron_migration_targets_v1():
    """T11.1.a: the schedule's function posts /v1/ingest-runs with a per-slot
    Idempotency-Key, and nothing in it reaches the legacy /ingest/ route."""
    name, args, returns, body = trigger_ingest()
    assert name == "20261007000010_schedule_v1.sql"
    assert (args, returns) == ("slug text default 'all', force boolean default false", "bigint")  # unchanged
    assert re.search(r"url\s*:=\s*rtrim\(api_url, '/'\) \|\| '/v1/ingest-runs',", body)
    assert "/ingest/" not in body and "?force" not in body
    key = re.search(r"'Idempotency-Key',\s*(.*?)\),\s*body", body, re.DOTALL).group(1)
    assert "'registerwatch-cron-'" in key and "now() at time zone 'UTC'" in key and "'YYYY-MM-DD-HH24'" in key
    assert "slug" in key and "'-force'" in key  # the key names the request: slot, target, force
    assert "jsonb_build_object('registers', jsonb_build_array(slug))" in body  # else {} for all
    # The daily job still calls it, at the same slots.
    jobs = re.findall(r"cron\.schedule\(\s*'registerwatch-daily',\s*'([^']*)',\s*\$\$(.*?)\$\$",
                      "".join(p.read_text() for p in sorted(SUPABASE.glob("*.sql"))), re.DOTALL)
    assert jobs[-1] == ("0 6,8,10 * * *", "select registerwatch_private.trigger_ingest()")
