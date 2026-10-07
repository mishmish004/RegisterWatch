"""Against a real Postgres: the migrations, every register's schema, the row
history, and the queries the scheduler and /status depend on.

Skipped unless REGISTERWATCH_TEST_DATABASE_URL points at a database this test
may wipe (it drops and recreates every register schema and public's tables):

    docker run -d --rm --name rw-pg -e POSTGRES_PASSWORD=rw -e POSTGRES_DB=rw \\
        -p 55432:5432 postgres:16-alpine
    REGISTERWATCH_TEST_DATABASE_URL=postgresql://postgres:rw@localhost:55432/rw uv run pytest tests/test_postgres.py
"""

from __future__ import annotations

import contextlib
import os
import pathlib

import pytest

DSN = os.environ.get("REGISTERWATCH_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="REGISTERWATCH_TEST_DATABASE_URL not set")

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def db():
    import psycopg
    from psycopg.rows import dict_row

    from registerwatch.registers import all_registers

    with psycopg.connect(DSN, autocommit=True) as c:
        for r in all_registers():
            c.execute(f"DROP SCHEMA IF EXISTS {r.slug} CASCADE")
        c.execute("DROP TABLE IF EXISTS raw_snapshots, sources, host_state CASCADE")
        for path in sorted((ROOT / "src" / "registerwatch" / "migrations").glob("*.sql")):
            c.execute(path.read_text())

    @contextlib.contextmanager
    def tx():
        with psycopg.connect(DSN, row_factory=dict_row) as conn:
            with conn.transaction():
                yield conn

    return tx


def test_every_register_schema_exists_with_its_tables_and_views(db):
    from registerwatch.registers import all_registers

    with db() as c:
        for r in all_registers():
            have = {row["table_name"] for row in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (r.slug,))}
            want = {t.name for t in r.tables} | {f"current_{t.name}" for t in r.tables}
            assert have == want, r.slug


def test_the_ddl_is_idempotent(db):
    """T4.1.b: every packaged migration applies twice over a migrated database,
    and the views expose the stable key."""
    from registerwatch.cli import SCHEMA_MIGRATION
    from registerwatch.db.schema import all_ddl
    from registerwatch.registers import all_registers

    with db() as c:
        c.execute(all_ddl(all_registers()))
        for _ in range(2):
            for path in sorted((ROOT / "src" / "registerwatch" / "migrations").glob("*.sql")):
                c.execute(path.read_text())
        assert (ROOT / "src" / "registerwatch" / "migrations" / SCHEMA_MIGRATION).exists()
        cols = [r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'gb_ukgc' "
            "AND table_name = 'current_licences' ORDER BY ordinal_position")]
        assert cols[-2:] == ["id", "first_seen_snapshot_id"]
        c.execute("SELECT id FROM gb_ukgc.current_licences LIMIT 1").fetchall()


def test_supabase_migrations_upgrade_a_database_made_by_older_ones():
    """Supabase applies each version once, in order: the newest register DDL must
    upgrade the views and indexes an older version created (T4.1.b)."""
    import psycopg

    supabase = ROOT / "supabase" / "migrations"
    # pg_cron/pg_net exist only on Supabase; that file schedules, it does not shape tables.
    files = [p for p in sorted(supabase.glob("*.sql")) if "schedule" not in p.name]
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS rw_upgrade")
        admin.execute("CREATE DATABASE rw_upgrade")
    try:
        with psycopg.connect(DSN.rsplit("/", 1)[0] + "/rw_upgrade", autocommit=True) as c:
            for path in files:
                c.execute(path.read_text())
            n = c.execute("SELECT count(*) FROM pg_indexes WHERE indexname = 'licences_current_id'").fetchone()[0]
            c.execute("SELECT id, first_seen_snapshot_id FROM gb_ukgc.current_licences LIMIT 1")
        assert [p.name for p in files][-1].endswith("_register_schemas.sql") and n >= 1
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute("DROP DATABASE IF EXISTS rw_upgrade")


def test_every_registers_real_rows_load_through_copy(db):
    """Types are where COPY breaks: dates, bigint, text[] arrays, Decimal."""
    from registerwatch.db.repos import observations
    from registerwatch.db.repos import snapshots as repo
    from registerwatch.ingest.engine import row_hashes
    from registerwatch.registers import all_registers
    from tests.test_registers import parsed

    with db() as c:
        for r in all_registers():
            src = repo.upsert_source(c, r.slug, r.name, r.country, r.config())
            sid = _snapshot(c, src["id"])
            rows = parsed(r.slug)
            stats = observations.apply(c, r, sid, {t: row_hashes(v) for t, v in rows.items()})
            for t, v in rows.items():
                assert stats[t]["inserted"] == len(v), (r.slug, t)
                n = c.execute(f"SELECT count(*) AS n FROM {r.slug}.current_{t}").fetchone()["n"]
                assert n == len(v), (r.slug, t)


def _snapshot(c, source_id: int, complete: bool = True) -> int:
    from datetime import datetime, timezone

    from registerwatch.db.repos import snapshots as repo

    return repo.insert_snapshot(
        c, source_id=source_id, run_started_at=datetime.now(timezone.utc), raw_hash=b"x", blob_ref="t",
        http_status=200, pages_expected=1, pages_ok=1, fetch_log=[], complete=complete,
        incomplete_reason=None if complete else "TEST", record_count=1 if complete else None,
        canonical_hash=None, parsed=complete)


def three_engine_runs(db, store_dir, monkeypatch):
    """gb_ukgc through the whole engine, real SQL, fake HTTP: a baseline, a run
    that revokes 103 and renumbers 102's licence, then a run that fails (503).
    Returns the three IngestResults."""
    import hashlib

    from registerwatch.ingest import engine
    from registerwatch.registers import gb_ukgc
    from registerwatch.storage.blobs import LocalBlobs
    from tests import test_engine as te

    monkeypatch.setattr(engine, "tx", db)
    monkeypatch.setattr(engine.limiter, "relax", lambda host: None)
    responses = {gb_ukgc.DOWNLOAD_URL: (200, te.PAGE)}
    responses.update({te.CSV.format(p): (200, b) for p, b in te.PARTS.items()})

    def fake_fetch_one(client, req, budget, validators=None):
        status, body = responses[req.url]
        return te.Fetched(req, status, body, hashlib.sha256(body).digest() if body else None,
                          {"content-type": "text/csv"}, [{"url": req.url, "status_code": status}])

    monkeypatch.setattr(engine, "fetch_one", fake_fetch_one)
    store = LocalBlobs(store_dir)
    run = lambda: engine.ingest(gb_ukgc.REGISTER, store, force=True)  # noqa: E731

    first = run()
    assert first.complete, first.reason
    responses[te.LIC] = (200, te.LICENCES
                         .replace(b'"103","000103-R-100000-001","Active"', b'"103","000103-R-100000-001","Revoked"')
                         .replace(b"000102-N-317976-010", b"000102-N-317976-011"))
    second = run()
    assert second.complete and not second.unchanged
    responses[te.LIC] = (503, None)
    third = run()
    assert not third.complete
    return first, second, third


def test_history_across_three_runs_through_the_engine(db, tmp_path, monkeypatch):
    """The whole engine, real SQL, fake HTTP: a change is one removal plus one
    insertion; an incomplete run changes nothing."""
    first, second, third = three_engine_runs(db, tmp_path, monkeypatch)

    with db() as c:
        rows = c.execute("SELECT licence_number, status, first_seen_snapshot_id AS f, removed_snapshot_id AS r "
                         "FROM gb_ukgc.licences ORDER BY id").fetchall()
        current = c.execute("SELECT licence_number, status FROM gb_ukgc.current_licences "
                            "ORDER BY licence_number").fetchall()
        changes = c.execute("SELECT count(*) AS n FROM gb_ukgc.licences WHERE first_seen_snapshot_id = %s "
                            "OR removed_snapshot_id = %s", (second.snapshot_id, second.snapshot_id)).fetchone()["n"]
        snap = c.execute("SELECT complete, record_count, canonical_hash, parsed_at FROM raw_snapshots "
                         "WHERE id = %s", (third.snapshot_id,)).fetchone()

    removed = {(r["licence_number"], r["status"]) for r in rows if r["r"] == second.snapshot_id}
    added = {(r["licence_number"], r["status"]) for r in rows if r["f"] == second.snapshot_id}
    assert removed == {("000102-N-317976-010", "Active"), ("000103-R-100000-001", "Active")}
    assert added == {("000102-N-317976-011", "Active"), ("000103-R-100000-001", "Revoked")}
    assert changes == 4  # the differ's input for the 2nd run: two out, two in
    assert len(current) == 3 and ("000103-R-100000-001", "Revoked") in {(r["licence_number"], r["status"]) for r in current}
    # The 503 run is on record and touched nothing.
    assert snap["complete"] is False and snap["record_count"] is None and snap["parsed_at"] is None


def test_health_and_refetch_queries(db):
    from registerwatch.db.repos import snapshots as repo

    with db() as c:
        src = repo.upsert_source(c, "zz_health", "t", "XX", {})
        assert not repo.fetched_recently(c, src["id"], 20)
        _snapshot(c, src["id"], complete=False)
        assert not repo.fetched_recently(c, src["id"], 20)  # a failed run does not block a retry
        _snapshot(c, src["id"], complete=True)
        assert repo.fetched_recently(c, src["id"], 20)
        h = next(r for r in repo.source_health(c) if r["slug"] == "zz_health")
        assert h["last_good"] is not None and h["failed_7d"] == 1
        assert repo.recent_snapshots(c, src["id"])[0]["complete"] is True
        assert repo.previous_pages_ok(c, src["id"]) == 1
        c.execute("DELETE FROM raw_snapshots WHERE source_id = %s", (src["id"],))
        c.execute("DELETE FROM sources WHERE id = %s", (src["id"],))


# --- reading back: the query layer the API and CLI share ------------------------------

@pytest.fixture(scope="module")
def loaded(db):
    """Every register's fixture rows applied once as a complete snapshot (idempotent)."""
    from registerwatch.db.repos import observations
    from registerwatch.db.repos import snapshots as repo
    from registerwatch.ingest.engine import row_hashes
    from registerwatch.registers import all_registers
    from tests.test_registers import parsed

    with db() as c:
        for r in all_registers():
            src = repo.upsert_source(c, r.slug, r.name, r.country, r.config())
            if not c.execute("SELECT 1 FROM raw_snapshots WHERE source_id = %s AND complete", (src["id"],)).fetchone():
                observations.apply(c, r, _snapshot(c, src["id"]),
                                   {t: row_hashes(v) for t, v in parsed(r.slug).items()})
    return db


def test_rows_filters_pages_and_refuses_unknown_columns(loaded):
    from registerwatch import query
    from registerwatch.registers import REGISTRY

    reg = REGISTRY["ca_kgc"]
    t = next(x for x in reg.tables if x.name == "operators")
    with loaded() as c:
        allrows = query.rows(c, reg, t, limit=1000)
        page = query.rows(c, reg, t, limit=5, offset=5)
        one = query.rows(c, reg, t, filters={"operator": "3-102-939256 SRL"})
        q = query.rows(c, reg, t, q="ZUMOSPIN")  # case-insensitive substring
        with pytest.raises(ValueError, match="unknown column"):
            query.rows(c, reg, t, filters={"nope": "x"})
        literal = query.rows(c, reg, t, q="%")  # LIKE metacharacters are escaped
    assert allrows["total"] == 229 and len(page["rows"]) == 5 and page["rows"][0] == allrows["rows"][5]
    assert one["total"] == 4 and {r["host"] for r in one["rows"]} >= {"zumospin.com"}
    assert q["total"] == 1 and literal["total"] == 0


def test_check_domain_across_jurisdictions(loaded):
    from registerwatch import query
    from registerwatch.registers import all_registers

    with loaded() as c:
        res = query.check_domain(c, "https://www.BET365.com/", all_registers())
    assert res["domain"] == "bet365.com"
    assert "CH" in res["blocked_in"] and "US-NJ" in res["licensed_in"]
    nj = next(m for m in res["matches"] if m["register"] == "us_nj_dge")
    assert nj["match"] == "subdomain" and nj["row"]["site"] == "nj.bet365.com"
    with pytest.raises(ValueError):
        with loaded() as c:
            query.check_domain(c, "not a domain", all_registers())


def test_search_finds_text_in_several_registers(loaded):
    from registerwatch import query
    from registerwatch.registers import all_registers

    with loaded() as c:
        hits = query.search(c, "betway", all_registers(), limit=3)
    regs = {h["register"] for h in hits}
    assert {"de_ggl"} <= regs and all(len(h["rows"]) <= 3 for h in hits)


def test_changes_reports_additions_and_removals_but_not_the_baseline(loaded):
    from datetime import datetime, timedelta, timezone

    from registerwatch import query
    from registerwatch.db.repos import observations
    from registerwatch.ingest.engine import row_hashes
    from registerwatch.registers import REGISTRY
    from tests.test_registers import parsed

    reg = REGISTRY["gr_hgc"]
    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    rows = parsed("gr_hgc")["licensees"]
    changed = rows[1:] + [{"company": "NEW OPERATOR LTD", "licence_type": "Betting (Type 1)"}]
    with loaded() as c:
        baseline = query.changes(c, reg, since)
        src = c.execute("SELECT id FROM sources WHERE slug = 'gr_hgc'").fetchone()["id"]
        observations.apply(c, reg, _snapshot(c, src), {"licensees": row_hashes(changed)})
        after = query.changes(c, reg, since)
    assert baseline["tables"] == {}  # the first load is not "50 new licensees"
    ch = after["tables"]["licensees"]
    assert [r["company"] for r in ch["added"]] == ["NEW OPERATOR LTD"]
    assert [r["company"] for r in ch["removed"]] == [rows[0]["company"]]
