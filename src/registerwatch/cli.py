"""Every stage runnable by hand. The API and the scheduler call these same
functions — a code path that only runs at 6am is a code path you never test.

  registerwatch jurisdictions                 what is covered
  registerwatch gb                            one jurisdiction: registers, tables, freshness
  registerwatch gb rows licences --where status=Active --limit 20
  registerwatch gb search bet365
  registerwatch gb changes --since 2026-10-01
  registerwatch gb ingest
  registerwatch search bet365 -j gb,de        across jurisdictions
  registerwatch check-domain bet365.com       licensed where, blocked where
  registerwatch ingest [all | gb de | pl_mf]  the daily job
  registerwatch status | migrate | ddl | serve
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import pathlib
import sys
from datetime import date, datetime, time, timezone
from importlib.resources import files
from typing import Any

from registerwatch import __version__, jurisdictions
from registerwatch.config import settings

MIGRATIONS = files("registerwatch") / "migrations"
SCHEMA_MIGRATION = "20261004000005_register_schemas.sql"
SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[2]  # only meaningful in a source checkout


# --- output ------------------------------------------------------------------

def _emit(rows: list[dict[str, Any]], fmt: str, *, width: int = 40) -> None:
    if fmt == "json":
        print(json.dumps(rows, default=str, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0])
    if fmt == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: _cell(v, None) for k, v in r.items()})
        return
    cells = [[_cell(r.get(c), width) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))


def _cell(v: Any, width: int | None) -> str:
    s = "" if v is None else (";".join(map(str, v)) if isinstance(v, list) else str(v))
    return s if width is None or len(s) <= width else s[: width - 1] + "…"


def _since(value: str) -> datetime:
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date: {value!r}") from None
    if isinstance(d, date) and not isinstance(d, datetime):
        d = datetime.combine(d, time())
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _registers_for(codes: str | None):
    from registerwatch.registers import all_registers

    if not codes:
        return all_registers()
    return [r for code in codes.split(",") for r in jurisdictions.resolve(code)]


def _targets(names: list[str]):
    """Register slugs, jurisdiction codes, or 'all' — in any mix."""
    from registerwatch.registers import REGISTRY, all_registers

    if not names or names == ["all"]:
        return all_registers()
    out = []
    for n in names:
        if n in REGISTRY:
            out.append(REGISTRY[n])
        else:
            out.extend(jurisdictions.resolve(n))
    return list({r.slug: r for r in out}.values())


# --- global commands -------------------------------------------------------------

def cmd_jurisdictions(args: argparse.Namespace) -> int:
    rows = [{"code": jurisdictions.cli_name(code), "name": jurisdictions.name(code),
             "registers": ", ".join(r.slug for r in regs),
             "kind": ", ".join(sorted({r.kind for r in regs}))}
            for code, regs in jurisdictions.by_code().items()]
    _emit(rows, args.format, width=60)
    return 0


def cmd_registers(args: argparse.Namespace) -> int:
    rows = [{"slug": r.slug, "jurisdiction": jurisdictions.cli_name(r.country), "kind": r.kind,
             "regulator": r.regulator, "tables": ", ".join(t.name for t in r.tables)}
            for r in _registers_for(args.jurisdiction)]
    _emit(rows, args.format, width=60)
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    from registerwatch.db.engine import tx
    from registerwatch.db.schema import all_ddl
    from registerwatch.registers import all_registers

    for path in sorted((p for p in MIGRATIONS.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name):
        with tx() as conn:
            conn.execute(path.read_text())
        print(f"applied {path.name}")
    # Also straight from the register modules, so a register added since the
    # last `ddl --write` still gets its schema. Idempotent.
    with tx() as conn:
        conn.execute(all_ddl(all_registers()))
    print("applied register schemas")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from registerwatch.ingest.engine import ingest_many
    from registerwatch.storage.blobs import make_store

    results = ingest_many(args.registers, make_store(args.root), force=args.force,
                          accept_count_delta=args.accept_count_delta)
    worst = 0
    for r in results:
        if r.skipped:
            print(f"{r.slug:<11} skipped (good snapshot within MIN_REFETCH_INTERVAL_H)")
            continue
        state = "complete" if r.complete else f"INCOMPLETE {r.reason}"
        tables = " ".join(f"{k}={v}" for k, v in r.tables.items())
        print(f"{r.slug:<11} snapshot={r.snapshot_id} parts={r.parts_ok}/{r.parts_expected} {state}"
              + (f" [{tables}]" if tables else "") + (" unchanged" if r.unchanged else ""))
        if not r.complete:
            worst = 1
    return worst


def cmd_status(args: argparse.Namespace) -> int:
    from registerwatch.db.engine import tx
    from registerwatch.db.repos import snapshots as repo

    wanted = {r.slug for r in args.registers}
    with tx() as conn:
        health = {r["slug"]: r for r in repo.source_health(conn)}
    now = datetime.now(timezone.utc)
    rows, stale_any = [], False
    for slug in sorted(wanted):
        h = health.get(slug, {})
        good = h.get("last_good")
        age = (now - good).total_seconds() / 3600 if good else None
        stale = age is None or age > settings().stale_after_h
        stale_any |= stale
        rows.append({"register": slug, "last_good": good.strftime("%Y-%m-%d %H:%M") if good else None,
                     "hours_ago": round(age, 1) if age is not None else None,
                     "failed_7d": h.get("failed_7d", 0), "last_reason": h.get("last_reason"),
                     "stale": "STALE" if stale else ""})
    _emit(rows, args.format, width=60)
    # Non-zero when stale, so this doubles as a check a monitor can run.
    return 1 if stale_any else 0


def cmd_search(args: argparse.Namespace) -> int:
    from registerwatch import query
    from registerwatch.db.engine import tx

    with tx() as conn:
        hits = query.search(conn, args.query, args.registers, limit=args.limit)
    if args.format == "json":
        _emit(hits, "json")
        return 0
    if not hits:
        print(f"no match for {args.query!r}")
        return 1
    for h in hits:
        print(f"\n{h['jurisdiction']} · {h['register']}.{h['table']} — {h['total']} match(es)")
        _emit(h["rows"], args.format)
    return 0


def cmd_check_domain(args: argparse.Namespace) -> int:
    from registerwatch import query
    from registerwatch.db.engine import tx

    with tx() as conn:
        res = query.check_domain(conn, args.domain, _registers_for(args.jurisdiction))
    if args.format == "json":
        print(json.dumps(res, default=str, ensure_ascii=False, indent=2))
        return 0
    print(f"{res['domain']}: licensed in {', '.join(res['licensed_in']) or 'none'}; "
          f"blocked in {', '.join(res['blocked_in']) or 'none'}")
    _emit([{"jurisdiction": m["jurisdiction"], "register": m["register"], "kind": m["kind"], "match": m["match"],
            "row": ", ".join(f"{k}={v}" for k, v in m["row"].items() if v and k not in ("first_seen_at", "last_seen_at"))}
           for m in res["matches"]], "table", width=90)
    return 0


def cmd_ddl(args: argparse.Namespace) -> int:
    from registerwatch.db.schema import all_ddl
    from registerwatch.registers import all_registers

    sql = all_ddl(all_registers())
    if not args.write:
        print(sql)
        return 0
    targets = (SOURCE_ROOT / "src" / "registerwatch" / "migrations" / SCHEMA_MIGRATION,
               SOURCE_ROOT / "supabase" / "migrations" / SCHEMA_MIGRATION)
    if not targets[0].parent.is_dir():
        print("ddl --write needs a source checkout; print it instead", file=sys.stderr)
        return 2
    for target in targets:
        target.write_text(sql)
        print(f"wrote {target.relative_to(SOURCE_ROOT)}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("registerwatch.api:app", host=args.host or settings().host, port=args.port or settings().port,
                log_level=settings().log_level.lower(), reload=args.reload)
    return 0


# --- per-jurisdiction commands ------------------------------------------------------

def cmd_jur_info(args: argparse.Namespace) -> int:
    info = jurisdictions.describe(args.code)
    if args.format == "json":
        print(json.dumps(info, default=str, ensure_ascii=False, indent=2))
        return 0
    print(f"{info['code']} — {info['name']}")
    for r in info["registers"]:
        print(f"\n  {r['slug']}  ({r['kind']})  {r['regulator']}: {r['name']}\n  {r['homepage']}")
        for t, cols in r["tables"].items():
            print(f"    {t}: {', '.join(cols)}")
    return 0


def cmd_jur_rows(args: argparse.Namespace) -> int:
    from registerwatch import query
    from registerwatch.db.engine import tx

    reg, table = query.find_table(args.registers, args.table)
    filters = dict(w.split("=", 1) for w in args.where)
    with tx() as conn:
        res = query.rows(conn, reg, table, q=args.q, filters=filters, limit=args.limit, offset=args.offset)
    if args.format == "table":
        print(f"{res['register']}.{res['table']}: {res['total']} row(s); showing {len(res['rows'])} from {res['offset']}")
    _emit(res["rows"], args.format)
    return 0


def cmd_jur_changes(args: argparse.Namespace) -> int:
    from registerwatch import query
    from registerwatch.db.engine import tx

    with tx() as conn:
        res = [query.changes(conn, r, args.since, limit=args.limit) for r in args.registers]
    if args.format == "json":
        print(json.dumps(res, default=str, ensure_ascii=False, indent=2))
        return 0
    any_change = False
    for r in res:
        for t, ch in r["tables"].items():
            any_change = True
            for kind in ("added", "removed"):
                if ch[kind]:
                    print(f"\n{r['register']}.{t} — {kind} {len(ch[kind])}")
                    _emit(ch[kind], "table")
    if not any_change:
        print(f"no changes since {args.since:%Y-%m-%d %H:%M} UTC")
    return 0


# --- parser -----------------------------------------------------------------------

def _fmt(p: argparse.ArgumentParser, default: str = "table") -> None:
    p.add_argument("--format", "-f", choices=("table", "csv", "json"), default=default)


def _ingest_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--force", action="store_true", help="ignore MIN_REFETCH_INTERVAL_H")
    p.add_argument("--accept-count-delta", action="store_true",
                   help="accept row counts that moved beyond tolerance (after checking them)")
    p.add_argument("--root", type=pathlib.Path, default=None,
                   help="local blob root (default: BLOB_BACKEND / SNAPSHOT_ROOT)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="registerwatch",
        description="Gambling regulators' public registers: scrape, store, query.",
        epilog="jurisdictions: " + " ".join(jurisdictions.cli_name(c) for c in jurisdictions.by_code())
               + "  — run `registerwatch <code> -h`",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"registerwatch {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    s = sub.add_parser("jurisdictions", help="list covered jurisdictions")
    _fmt(s)
    s.set_defaults(fn=cmd_jurisdictions)

    s = sub.add_parser("registers", help="list registers and their schemas")
    s.add_argument("-j", "--jurisdiction", help="comma-separated codes, e.g. gb,de")
    _fmt(s)
    s.set_defaults(fn=cmd_registers)

    s = sub.add_parser("ingest", help="fetch, validate and record (the daily job)")
    s.add_argument("targets", nargs="*", default=["all"], help="'all', jurisdiction codes, or register slugs")
    _ingest_flags(s)
    s.set_defaults(fn=cmd_ingest, resolve=lambda a: _targets(a.targets))

    s = sub.add_parser("status", help="freshness per register; exit 1 if any is stale")
    s.add_argument("-j", "--jurisdiction")
    _fmt(s)
    s.set_defaults(fn=cmd_status, resolve=lambda a: _registers_for(a.jurisdiction))

    s = sub.add_parser("search", help="find text in every register's current rows")
    s.add_argument("query")
    s.add_argument("-j", "--jurisdiction")
    s.add_argument("--limit", type=int, default=10, help="rows shown per table")
    _fmt(s)
    s.set_defaults(fn=cmd_search, resolve=lambda a: _registers_for(a.jurisdiction))

    s = sub.add_parser("check-domain", help="where a domain is licensed, and where it is blocked")
    s.add_argument("domain")
    s.add_argument("-j", "--jurisdiction")
    _fmt(s)
    s.set_defaults(fn=cmd_check_domain)

    s = sub.add_parser("migrate", help="apply migrations and every register's schema")
    s.set_defaults(fn=cmd_migrate)

    s = sub.add_parser("ddl", help="print the generated register schemas")
    s.add_argument("--write", action="store_true", help="write the schema migration (source checkout)")
    s.set_defaults(fn=cmd_ddl)

    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(fn=cmd_serve)

    for code, regs in jurisdictions.by_code().items():
        name = jurisdictions.cli_name(code)
        j = sub.add_parser(name, help=f"{jurisdictions.name(code)}: {', '.join(r.slug for r in regs)}",
                           description=f"{jurisdictions.name(code)} — {', '.join(r.regulator for r in regs)}")
        j.set_defaults(fn=cmd_jur_info, code=code, format="table",
                       resolve=lambda a, c=code: jurisdictions.resolve(c))
        jsub = j.add_subparsers(dest="action", metavar="ACTION")

        a = jsub.add_parser("info", help="registers, tables and columns (default)")
        _fmt(a)
        a.set_defaults(fn=cmd_jur_info)

        a = jsub.add_parser("ingest", help=f"ingest {name}'s registers now")
        _ingest_flags(a)
        a.set_defaults(fn=cmd_ingest)

        a = jsub.add_parser("status", help="freshness of this jurisdiction's registers")
        _fmt(a)
        a.set_defaults(fn=cmd_status)

        tables = [t.name if sum(t.name == u.name for r2 in regs for u in r2.tables) == 1 else f"{r.slug}.{t.name}"
                  for r in regs for t in r.tables]
        a = jsub.add_parser("rows", help="current rows of a table", description="tables: " + ", ".join(tables))
        a.add_argument("table", help=" | ".join(tables))
        a.add_argument("--where", "-w", action="append", default=[], metavar="COLUMN=VALUE",
                       help="exact-value filter; repeatable")
        a.add_argument("-q", help="substring in any text column")
        a.add_argument("--limit", type=int, default=50)
        a.add_argument("--offset", type=int, default=0)
        _fmt(a)
        a.set_defaults(fn=cmd_jur_rows)

        a = jsub.add_parser("search", help="find text in this jurisdiction's registers")
        a.add_argument("query")
        a.add_argument("--limit", type=int, default=10)
        _fmt(a)
        a.set_defaults(fn=cmd_search)

        a = jsub.add_parser("changes", help="rows added or removed since a date")
        a.add_argument("--since", type=_since, required=True, help="ISO date, e.g. 2026-10-01")
        a.add_argument("--limit", type=int, default=200)
        _fmt(a)
        a.set_defaults(fn=cmd_jur_changes)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=settings().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if hasattr(args, "resolve"):
            args.registers = args.resolve(args)
        return args.fn(args)
    except KeyError as exc:  # unknown jurisdiction, register or table
        print(exc.args[0], file=sys.stderr)
        return 2
    except ValueError as exc:  # bad filter column, bad domain
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
