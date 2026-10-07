"""One Postgres schema per register, generated from its Table declarations.

Every register table has the same history columns in front of its own:

  row_hash               sha256 of the row's values (+ occurrence, for exact
                         duplicates), the row's identity
  first_seen_snapshot_id the complete snapshot that first contained the row
  last_seen_snapshot_id  the newest complete snapshot that still contained it
  removed_snapshot_id    the first complete snapshot that no longer did; NULL
                         while the row is current

So storage grows with changes, not with days, and "what changed on the 3rd" is
`WHERE first_seen_snapshot_id = x OR removed_snapshot_id = x` — the differ
falls out of the storage model. `current_<table>` views show the register as of
its latest complete snapshot; they end with `id` and `first_seen_snapshot_id`,
the stable key API pages are keyed on. New view columns only ever go at the
end, so `CREATE OR REPLACE VIEW` can upgrade a view an older migration made.

The DDL is idempotent: CREATE ... IF NOT EXISTS, and ADD COLUMN IF NOT EXISTS
for every declared column, so a register that gains a column migrates forward
by re-running `registerwatch migrate`. Removing or retyping a column is a
human's job — the generator will not drop data.

Each table also gets the indexes the API's lookups use (`lookup_indexes`):
trigram indexes for substring search, and hostname indexes for domain checks.
"""

from __future__ import annotations

from registerwatch.registers.base import DOMAIN_COLUMNS, Register, Table

SQL_TYPE = {
    "text": "text", "date": "date", "timestamptz": "timestamptz", "int": "integer",
    "bigint": "bigint", "numeric": "numeric", "bool": "boolean", "text[]": "text[]",
}

HISTORY_COLUMNS = """\
  id                     bigserial PRIMARY KEY,
  row_hash               bytea  NOT NULL,
  first_seen_snapshot_id bigint NOT NULL REFERENCES public.raw_snapshots(id),
  last_seen_snapshot_id  bigint NOT NULL REFERENCES public.raw_snapshots(id),
  removed_snapshot_id    bigint REFERENCES public.raw_snapshots(id),
  first_seen_at          timestamptz NOT NULL DEFAULT now(),
  last_seen_at           timestamptz NOT NULL DEFAULT now(),
  removed_at             timestamptz"""


# What the read queries need from Postgres to use an index (plan.md P7.3):
#   pg_trgm, for `q`: a substring match (ILIKE '%x%') can only use a trigram
#     index. It goes in `extensions`, where Supabase keeps extensions; plain
#     Postgres gets that schema too.
#   array_text, for `q` on text[] columns: array_to_string is only STABLE, so
#     no index may use it, though joining text never changes. Kept out of
#     `public`, which Supabase serves over PostgREST.
SEARCH_SUPPORT = """\
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
CREATE SCHEMA IF NOT EXISTS registerwatch_private;
REVOKE ALL ON SCHEMA registerwatch_private FROM PUBLIC;
CREATE OR REPLACE FUNCTION registerwatch_private.array_text(text[]) RETURNS text
  LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$ SELECT array_to_string($1, ' ') $$;
"""
ARRAY_TEXT = "registerwatch_private.array_text"


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def lookup_indexes(schema: str, table: Table) -> list[str]:
    """Indexes the read queries use, on current rows only (what every one of them
    reads): a trigram index per searched column, for `q`; and per hostname
    column, `lower(x)` for an exact domain match and `reverse(lower(x))` for a
    subdomain match, a suffix turned into a prefix a btree can find."""
    out = []
    current = f"ON {schema}.{table.name}"
    for c in table.columns:
        if c.searched:
            expr = c.name if c.type == "text" else f"{ARRAY_TEXT}({c.name})"
            out.append(f"CREATE INDEX IF NOT EXISTS {table.name}_{c.name}_trgm\n  {current} "
                       f"USING gin ({expr} extensions.gin_trgm_ops) WHERE removed_snapshot_id IS NULL;")
        if c.name in DOMAIN_COLUMNS and c.type == "text":
            out.append(f"CREATE INDEX IF NOT EXISTS {table.name}_{c.name}_lower\n  {current} "
                       f"(lower({c.name})) WHERE removed_snapshot_id IS NULL;")
            out.append(f"CREATE INDEX IF NOT EXISTS {table.name}_{c.name}_reverse\n  {current} "
                       f"(reverse(lower({c.name})) text_pattern_ops) WHERE removed_snapshot_id IS NULL;")
    return out


def table_ddl(schema: str, table: Table) -> str:
    cols = ",\n".join(f"  {c.name:<22} {SQL_TYPE[c.type]}" for c in table.columns)
    adds = "\n".join(
        f"ALTER TABLE {schema}.{table.name} ADD COLUMN IF NOT EXISTS {c.name} {SQL_TYPE[c.type]};"
        for c in table.columns
    )
    view_cols = ", ".join(c.name for c in table.columns)
    lookups = "".join(f"{i}\n" for i in lookup_indexes(schema, table))
    return f"""\
CREATE TABLE IF NOT EXISTS {schema}.{table.name} (
{HISTORY_COLUMNS},
{cols}
);
{adds}
CREATE UNIQUE INDEX IF NOT EXISTS {table.name}_current_row
  ON {schema}.{table.name} (row_hash) WHERE removed_snapshot_id IS NULL;
CREATE INDEX IF NOT EXISTS {table.name}_first_seen ON {schema}.{table.name} (first_seen_snapshot_id);
CREATE INDEX IF NOT EXISTS {table.name}_removed
  ON {schema}.{table.name} (removed_snapshot_id) WHERE removed_snapshot_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS {table.name}_current_id
  ON {schema}.{table.name} (id) WHERE removed_snapshot_id IS NULL;
{lookups}CREATE OR REPLACE VIEW {schema}.current_{table.name} AS
  SELECT {view_cols}, first_seen_at, last_seen_at, id, first_seen_snapshot_id
    FROM {schema}.{table.name} WHERE removed_snapshot_id IS NULL;
COMMENT ON TABLE {schema}.{table.name} IS {_lit(table.description or table.name)};
"""


def register_ddl(register: Register) -> str:
    s = register.slug
    head = f"""\
-- {register.slug}: {register.name}
-- {register.regulator} ({register.country}) — {register.homepage}
CREATE SCHEMA IF NOT EXISTS {s};
COMMENT ON SCHEMA {s} IS {_lit(f"{register.regulator} ({register.country}): {register.name}")};
"""
    return head + "\n".join(table_ddl(s, t) for t in register.tables)


def all_ddl(registers) -> str:
    banner = """\
-- GENERATED by `registerwatch ddl --write` from src/registerwatch/registers/*.
-- Edit the register modules, not this file; tests fail when the two disagree.
-- Idempotent: safe to re-run.

"""
    return banner + SEARCH_SUPPORT + "\n" + "\n".join(register_ddl(r) for r in registers)
