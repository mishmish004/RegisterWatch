"""HTTP face of the same functions the CLI runs.

Supabase cannot run Python, so the daily schedule lives in Supabase (pg_cron +
pg_net, supabase/migrations/*_schedule_*.sql) and the work lives here: pg_cron
POSTs /ingest/all, this answers 202 at once and runs every register in the
background.

Operations (bearer INGEST_TOKEN)
  POST /ingest/{slug|all}                    ?force ?accept_count_delta ?wait
  POST /jurisdictions/{code}/ingest          every register of one jurisdiction
  GET  /ingest/last

Reading (open, or bearer READ_TOKEN when set)
  GET  /jurisdictions                        codes, names, registers
  GET  /jurisdictions/{code}                 registers, tables, columns, freshness
  GET  /jurisdictions/{code}/{slug}/{table}  current rows; ?q= ?limit ?offset ?<column>=<value>
  GET  /jurisdictions/{code}/changes         rows added/removed ?since=YYYY-MM-DD
  GET  /search?q=                            across every register ?jurisdiction=GB,DE
  GET  /check/domain/{domain}                licensed where, blocked where

Health
  GET  /health   GET /status   GET /registers
"""

from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response

from registerwatch import __version__, jurisdictions, query, registers
from registerwatch.config import settings
from registerwatch.db.engine import pool, tx
from registerwatch.db.repos import snapshots as repo
from registerwatch.ingest import engine
from registerwatch.storage.blobs import make_store

log = logging.getLogger(__name__)

# One batch at a time per process. The retry slots and a hand-fired run can
# overlap; the second is refused rather than queued behind the first.
_running = threading.Lock()
_last: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(
        level=settings().log_level,  # also fails fast on a bad .env
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    yield
    if pool.cache_info().currsize:
        pool().close()


app = FastAPI(
    title="registerwatch",
    version=__version__,
    description="Gambling regulators' public registers, one schema per register, with history.",
    lifespan=lifespan,
)


def require_token(authorization: str | None = Header(default=None)) -> None:
    token = settings().ingest_token
    if not token:
        raise HTTPException(503, "INGEST_TOKEN is not configured; ingest endpoint disabled")
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "bad or missing bearer token")


def require_read(authorization: str | None = Header(default=None)) -> None:
    token = settings().read_token
    if not token:
        return
    ok = [f"Bearer {t}" for t in (token, settings().ingest_token) if t]
    if not authorization or not any(secrets.compare_digest(authorization, o) for o in ok):
        raise HTTPException(401, "bad or missing bearer token")


def _registers_of(code: str):
    try:
        return jurisdictions.resolve(code)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from None


@app.get("/health", tags=["health"])
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/registers", tags=["health"])
def list_registers() -> list[dict[str, Any]]:
    return [{"slug": r.slug, "country": r.country, "regulator": r.regulator, "name": r.name,
             "kind": r.kind, "homepage": r.homepage, "schema": r.slug,
             "tables": {t.name: t.column_names for t in r.tables}} for r in registers.all_registers()]


@app.get("/status", tags=["health"])
def status(response: Response) -> dict[str, Any]:
    try:
        with tx() as conn:
            rows = {r["slug"]: r for r in repo.source_health(conn)}
    except Exception as exc:  # noqa: BLE001 — an unreachable database is the stalest state of all
        log.exception("status: database unreachable")
        response.status_code = 503
        return {"stale": True, "error": f"database unreachable: {type(exc).__name__}"}
    now = datetime.now(timezone.utc)
    sources = []
    for reg in registers.all_registers():
        r = rows.get(reg.slug, {})
        last_good = r.get("last_good")
        age_h = (now - last_good).total_seconds() / 3600 if last_good else None
        sources.append({
            "slug": reg.slug, "country": reg.country,
            "last_fetch": r.get("last_fetch"), "last_good": last_good,
            "hours_since_good": round(age_h, 1) if age_h is not None else None,
            "last_reason": r.get("last_reason"), "failed_7d": r.get("failed_7d", 0),
            "stale": age_h is None or age_h > settings().stale_after_h,
        })
    stale = [s["slug"] for s in sources if s["stale"]]
    if stale:
        response.status_code = 503
    return {"stale": bool(stale), "stale_registers": stale,
            "stale_after_h": settings().stale_after_h, "sources": sources}


@app.post("/ingest/{slug}", dependencies=[Depends(require_token)], status_code=202, tags=["ingest"])
def trigger(
    slug: str,
    background: BackgroundTasks,
    response: Response,
    force: bool = False,
    accept_count_delta: bool = False,
    wait: bool = False,
) -> dict[str, Any]:
    try:
        targets = registers.all_registers() if slug == "all" else [registers.get(slug)]
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from None
    if not _running.acquire(blocking=False):
        raise HTTPException(409, "an ingest is already running")
    if wait:
        response.status_code = 200
        return _run_and_release(targets, force, accept_count_delta)
    background.add_task(_run_and_release, targets, force, accept_count_delta)
    return {"accepted": True, "registers": [r.slug for r in targets],
            "force": force, "accept_count_delta": accept_count_delta}


@app.get("/ingest/last", dependencies=[Depends(require_token)], tags=["ingest"])
def last_run() -> dict[str, Any]:
    if not _last:
        raise HTTPException(404, "no run has finished in this process yet")
    return _last


@app.post("/jurisdictions/{code}/ingest", dependencies=[Depends(require_token)], status_code=202,
          tags=["ingest"])
def trigger_jurisdiction(code: str, background: BackgroundTasks, response: Response, force: bool = False,
                         accept_count_delta: bool = False, wait: bool = False) -> dict[str, Any]:
    targets = _registers_of(code)
    if not _running.acquire(blocking=False):
        raise HTTPException(409, "an ingest is already running")
    if wait:
        response.status_code = 200
        return _run_and_release(targets, force, accept_count_delta)
    background.add_task(_run_and_release, targets, force, accept_count_delta)
    return {"accepted": True, "registers": [r.slug for r in targets],
            "force": force, "accept_count_delta": accept_count_delta}


# --- reading -------------------------------------------------------------------

@app.get("/jurisdictions", dependencies=[Depends(require_read)], tags=["read"])
def list_jurisdictions() -> list[dict[str, Any]]:
    return [{"code": code, "name": jurisdictions.name(code),
             "registers": [{"slug": r.slug, "regulator": r.regulator, "kind": r.kind} for r in regs]}
            for code, regs in jurisdictions.by_code().items()]


@app.get("/jurisdictions/{code}", dependencies=[Depends(require_read)], tags=["read"])
def get_jurisdiction(code: str) -> dict[str, Any]:
    _registers_of(code)
    out = jurisdictions.describe(code)
    try:
        with tx() as conn:
            health = {r["slug"]: r for r in repo.source_health(conn)}
        for reg in out["registers"]:
            h = health.get(reg["slug"], {})
            reg["last_good"] = h.get("last_good")
            reg["last_reason"] = h.get("last_reason")
    except Exception:  # noqa: BLE001 — the description is still useful without freshness
        log.warning("freshness unavailable for %s", code, exc_info=True)
    return out


@app.get("/jurisdictions/{code}/changes", dependencies=[Depends(require_read)], tags=["read"])
def get_changes(code: str, since: datetime = Query(..., description="ISO date or timestamp"),
                limit: int = 200) -> dict[str, Any]:
    regs = _registers_of(code)
    since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    with tx() as conn:
        return {"jurisdiction": jurisdictions.normalise(code), "since": since,
                "registers": [query.changes(conn, r, since, limit=limit) for r in regs]}


@app.get("/jurisdictions/{code}/{slug}/{table}", dependencies=[Depends(require_read)], tags=["read"])
def get_rows(code: str, slug: str, table: str, request: Request, q: str | None = None,
             limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Current rows. Any other query parameter filters a column by exact value,
    e.g. `/jurisdictions/gb/gb_ukgc/licences?status=Active&type=Remote`."""
    regs = _registers_of(code)
    try:
        reg, tbl = query.find_table([r for r in regs if r.slug == slug], table)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from None
    filters = {k: v for k, v in request.query_params.items() if k not in ("q", "limit", "offset")}
    try:
        with tx() as conn:
            return query.rows(conn, reg, tbl, q=q, filters=filters, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/search", dependencies=[Depends(require_read)], tags=["read"])
def search(q: str = Query(..., min_length=2), jurisdiction: str | None = None,
           limit: int = 20) -> dict[str, Any]:
    regs = ([r for code in jurisdiction.split(",") for r in _registers_of(code)]
            if jurisdiction else registers.all_registers())
    with tx() as conn:
        hits = query.search(conn, q, regs, limit=limit)
    return {"q": q, "tables": len(hits), "matches": sum(h["total"] for h in hits), "results": hits}


@app.get("/check/domain/{domain}", dependencies=[Depends(require_read)], tags=["read"])
def check_domain(domain: str, jurisdiction: str | None = None) -> dict[str, Any]:
    regs = ([r for code in jurisdiction.split(",") for r in _registers_of(code)]
            if jurisdiction else registers.all_registers())
    try:
        with tx() as conn:
            return query.check_domain(conn, domain, regs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def _run_and_release(targets, force: bool, accept_count_delta: bool) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    try:
        results = engine.ingest_many(targets, make_store(), force=force, accept_count_delta=accept_count_delta)
        outcome = {"ok": True, "results": [r.as_dict() for r in results],
                   "complete": sum(r.complete for r in results),
                   "incomplete": [r.slug for r in results if not r.complete and not r.skipped],
                   "skipped": [r.slug for r in results if r.skipped]}
    except Exception as exc:  # noqa: BLE001 — a background task has nobody to raise to
        log.exception("ingest batch failed")
        outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _running.release()
    _last.clear()
    _last.update({"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), **outcome})
    return dict(_last)
