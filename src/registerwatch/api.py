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

The v1 surface (registerwatch.http) is mounted here too: /v1/..., and the
unversioned probes /livez and /readyz.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import anyio
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response, Security

from registerwatch import __version__, jurisdictions, query, registers
from registerwatch.config import settings
from registerwatch.db.engine import close_pools, thread_limit, tx
from registerwatch.db.repos import snapshots as repo
from registerwatch.http import caching, deps, guards, openapi, probes, problems, ratelimit, request_id, v1
from registerwatch.ingest import engine, runs
from registerwatch.storage.blobs import make_store

log = logging.getLogger(__name__)

# One batch at a time per process. The retry slots and a hand-fired run can
# overlap; the second is refused rather than queued behind the first. The same
# lock v1 runs take (with a cross-process one), so legacy and v1 never overlap here.
_running = runs.LOCAL
_last: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    request_id.install_log_field()
    logging.basicConfig(
        level=settings().log_level,  # also fails fast on a bad .env
        format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
    )
    # Sync handlers run in anyio's worker threads; size them to the read pool (P8.2).
    anyio.to_thread.current_default_thread_limiter().total_tokens = thread_limit()
    runs.stop_on_sigterm()
    runs.sweep_on_startup()
    yield
    close_pools()


app = FastAPI(
    title="registerwatch",
    version=__version__,
    description="Gambling regulators' public registers, one schema per register, with history.",
    lifespan=lifespan,
    # Relative: the deployment serving this spec. Clients set their own base URL.
    servers=[{"url": "/", "description": "The deployment serving this document"}],
    contact={"name": "registerwatch", "url": "https://github.com/mishmish004/RegisterWatch"},
    # The repository has no LICENSE file, so no rights are granted beyond reading the code.
    license_info={"name": "All rights reserved", "url": "https://choosealicense.com/no-permission/"},
)
app.include_router(v1.router)
app.include_router(probes.router)
openapi.install(app)
problems.install(app)
caching.install(app)
# Each add wraps the ones before it, so the request id is outermost: it is set
# before anything can answer, and every answer, a 500 included, carries it.
# Rate limiting wraps everything that answers a request (a 500 too, so it carries
# the client's RateLimit fields) and is inside no-store, so an ingest 429 is not stored.
app.add_middleware(guards.NulGuardMiddleware)
app.add_middleware(problems.UnhandledErrorMiddleware)
app.add_middleware(ratelimit.RateLimitMiddleware)
app.add_middleware(caching.NoStoreMiddleware)
app.add_middleware(request_id.RequestIdMiddleware)

# Postgres' bigint: the most a legacy LIMIT or OFFSET can be without a 500.
_PG_BIGINT = 2**63 - 1
# Any route answers this, even one without parameters (http/guards.py).
_NUL = {400: {"description": "A parameter or the path carried a NUL byte"}}
# Routes anyone may call, declared so rather than left without a security requirement.
_OPEN = {"security": []}


# The legacy checks compare the raw header, as they always have; the schemes
# are declared so the spec says how these routes authenticate (Phase 6).
def require_token(request: Request, _: object = Security(deps.INGEST_TOKEN)) -> None:
    token = settings().ingest_token
    if not token:
        raise HTTPException(503, "INGEST_TOKEN is not configured; ingest endpoint disabled")
    authorization = request.headers.get("authorization")
    if not authorization or not any(deps.token_matches(authorization, f"Bearer {token}")):
        raise HTTPException(401, "bad or missing bearer token")


def require_read(request: Request, _r: object = Security(deps.READ_TOKEN),
                 _i: object = Security(deps.INGEST_TOKEN)) -> None:
    token = settings().read_token
    if not token:
        return
    authorization = request.headers.get("authorization")
    ok = [f"Bearer {t}" for t in (token, settings().ingest_token) if t]
    if not authorization or not any(deps.token_matches(authorization, *ok)):
        raise HTTPException(401, "bad or missing bearer token")


def _registers_of(code: str):
    try:
        return jurisdictions.resolve(code)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from None


@app.get("/health", tags=["health"], responses=_NUL, openapi_extra=_OPEN)
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/registers", tags=["health"], responses=_NUL, openapi_extra=_OPEN)
def list_registers() -> list[dict[str, Any]]:
    return [{"slug": r.slug, "country": r.country, "regulator": r.regulator, "name": r.name,
             "kind": r.kind, "homepage": r.homepage, "schema": r.slug,
             "tables": {t.name: t.column_names for t in r.tables}} for r in registers.all_registers()]


@app.get("/status", tags=["health"], responses=_NUL, openapi_extra=_OPEN)
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


@app.get("/ingest/last", dependencies=[Depends(require_token)], tags=["ingest"], responses=_NUL)
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

@app.get("/jurisdictions", dependencies=[Depends(require_read)], tags=["read"], responses=_NUL)
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
                limit: int = Query(200, ge=0, le=_PG_BIGINT)) -> dict[str, Any]:
    regs = _registers_of(code)
    since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    with tx() as conn:
        return {"jurisdiction": jurisdictions.normalise(code), "since": since,
                "registers": [query.changes(conn, r, since, limit=limit) for r in regs]}


@app.get("/jurisdictions/{code}/{slug}/{table}", dependencies=[Depends(require_read)], tags=["read"])
def get_rows(code: str, slug: str, table: str, request: Request, q: str | None = None,
             limit: int = 100, offset: int = Query(0, ge=0, le=_PG_BIGINT)) -> dict[str, Any]:
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
