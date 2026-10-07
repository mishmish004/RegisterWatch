"""The committed schema migration is generated, and must stay generated."""

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
