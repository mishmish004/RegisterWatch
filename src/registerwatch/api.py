"""HTTP face of the same functions the CLI runs.

Supabase cannot run Python, so the daily schedule lives in Supabase (pg_cron +
pg_net, supabase/migrations/*_schedule_*.sql) and the work lives here: pg_cron
POSTs /ingest/all, this answers 202 at once and runs every register in the
background.

Operations (bearer INGEST_TOKEN)
  POST /ingest/{slug|all}                    ?force ?accept_count_delta ?wait
  POST /jurisdictions/{code}/ingest          every register of one jurisdiction
  GET  /ingest/last

  POST /build                                rebuild the model now (ingest batches do it themselves)

Reading the registers (open, or bearer READ_TOKEN when set)
  GET  /jurisdictions                        codes, names, registers
  GET  /jurisdictions/{code}                 registers, tables, columns, freshness
  GET  /jurisdictions/{code}/{slug}/{table}  current rows; ?q= ?limit ?offset ?<column>=<value>
  GET  /jurisdictions/{code}/changes         rows added/removed ?since=YYYY-MM-DD
  GET  /search?q=                            across every register ?jurisdiction=GB,DE
  GET  /check/domain/{domain}                licensed where, blocked where

Reading the model (same auth): one answer across every register, with evidence
  GET  /domains/{domain}                     a verdict per jurisdiction, never a bare yes/no
  GET  /operators?q=                         companies by name, trading name or website
  GET  /operators/{operator_id}              footprint, licences, brands, websites, flags, history
  GET  /licences                             ?jurisdiction ?status ?product ?q ?current
  GET  /events                               the change feed ?since ?type ?jurisdiction ?operator ?domain
  GET  /jurisdictions/{code}/profile         coverage, counts, products, recent changes
  GET  /coverage                             every register's coverage, freshness, data quality

Health
  GET  /health   GET /status   GET /registers

Web
  GET  /                                     a lookup page over the model endpoints (web/)
"""

from __future__ import annotations

import logging
import pathlib
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from registerwatch import __version__, intel, jurisdictions, query, registers
from registerwatch.config import settings
from registerwatch.db.engine import pool, tx
from registerwatch.db.repos import model as model_repo
from registerwatch.db.repos import snapshots as repo
from registerwatch.ingest import engine
from registerwatch.storage.blobs import make_store

log = logging.getLogger(__name__)

# One batch at a time per process. The retry slots and a hand-fired run can
# overlap; the second is refused rather than queued behind the first.
_running = threading.Lock()
_last: dict[str, Any] = {}

# A batch records its snapshots first and builds the model after, so the model
# trails the registers by minutes during a run. Beyond this it is stale.
MODEL_GRACE = timedelta(hours=1)


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


WEB = pathlib.Path(__file__).parent / "web"
# The page draws register data; nothing it loads may come from anywhere else.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
app.mount("/web", StaticFiles(directory=WEB), name="web")


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    """The lookup page. Static: it reads the same endpoints as any client, with
    the read token when one is set."""
    return HTMLResponse((WEB / "index.html").read_text(), headers={"Content-Security-Policy": CSP})


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
    model = _model_freshness(now)
    if stale or model.get("stale"):
        response.status_code = 503
    return {"stale": bool(stale) or bool(model.get("stale")), "stale_registers": stale,
            "stale_after_h": settings().stale_after_h, "model": model, "sources": sources}


def _model_freshness(now: datetime) -> dict[str, Any]:
    """Stale when a complete snapshot has waited more than MODEL_GRACE for a
    build: the registers moved and the model's answers did not."""
    try:
        with tx() as conn:
            f = model_repo.freshness(conn)
    except Exception as exc:  # noqa: BLE001 — reported, not fatal: the registers' health stands alone
        log.warning("model freshness unavailable", exc_info=True)
        return {"error": f"{type(exc).__name__}"}
    newest = f["newest_snapshot_at"]
    f["stale"] = bool(f["behind"] and newest and now - newest > MODEL_GRACE)
    return f


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


@app.post("/build", dependencies=[Depends(require_token)], tags=["ingest"])
def trigger_build() -> dict[str, Any]:
    """Rebuild the model from the register schemas, inline (seconds)."""
    if not _running.acquire(blocking=False):
        raise HTTPException(409, "an ingest or build is already running")
    try:
        return _rebuild()
    finally:
        _running.release()


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


# --- reading the model ----------------------------------------------------------------

def _regs_param(jurisdiction: str | None):
    return ([r for code in jurisdiction.split(",") for r in _registers_of(code)]
            if jurisdiction else registers.all_registers())


def _today():
    return datetime.now(timezone.utc).date()


@app.get("/domains/{domain:path}", dependencies=[Depends(require_read)], tags=["model"])
def get_domain(domain: str, jurisdiction: str | None = None) -> dict[str, Any]:
    """Per jurisdiction: authorised, blocked, listed but not operating, related
    host listed, previously listed, not listed, no data… with the explanation,
    caveats and register rows behind each; plus the same name elsewhere and the
    host's history. `domain` may be a URL."""
    regs = _regs_param(jurisdiction)
    try:
        with tx() as conn:
            return intel.domain(conn, domain, regs, today=_today())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/operators", dependencies=[Depends(require_read)], tags=["model"])
def search_operators(q: str = Query(..., min_length=2), jurisdiction: str | None = None,
                     limit: int = 20) -> dict[str, Any]:
    with tx() as conn:
        return intel.search_operators(conn, q, _regs_param(jurisdiction), limit=limit)


@app.get("/operators/{operator_id}", dependencies=[Depends(require_read)], tags=["model"])
def get_operator(operator_id: str, jurisdiction: str | None = None) -> dict[str, Any]:
    try:
        with tx() as conn:
            return intel.operator(conn, operator_id, _regs_param(jurisdiction), today=_today())
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from None


@app.get("/licences", dependencies=[Depends(require_read)], tags=["model"])
def get_licences(jurisdiction: str | None = None, status: str | None = None, product: str | None = None,
                 q: str | None = None, current: bool | None = True, limit: int = 100,
                 offset: int = 0) -> dict[str, Any]:
    """`status` and `product` take comma-separated values from the shared
    vocabularies (active, suspended, revoked… / casino, betting, poker…);
    `current=false` lists licences removed from their register."""
    with tx() as conn:
        return intel.licences(conn, _regs_param(jurisdiction), status=status, product=product, q=q,
                              current=current, limit=limit, offset=offset)


@app.get("/events", dependencies=[Depends(require_read)], tags=["model"])
def get_events(since: datetime | None = Query(None, description="ISO date or timestamp; default 7 days ago"),
               until: datetime | None = None, type: str | None = None, jurisdiction: str | None = None,
               operator: str | None = None, domain: str | None = None, limit: int = 100,
               offset: int = 0) -> dict[str, Any]:
    """`type`: comma-separated event types (licence.status_changed) or kinds
    (licence, domain, block, party, brand)."""
    since = _utc(since) if since else datetime.now(timezone.utc) - timedelta(days=7)
    try:
        with tx() as conn:
            out = intel.events(conn, _regs_param(jurisdiction), since=since, until=_utc(until) if until else None,
                               types=type.split(",") if type else None, operator_id=operator, host=domain,
                               limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"since": since, **out}


@app.get("/jurisdictions/{code}/profile", dependencies=[Depends(require_read)], tags=["model"])
def get_jurisdiction_profile(code: str) -> dict[str, Any]:
    _registers_of(code)
    with tx() as conn:
        return intel.jurisdiction(conn, code, today=_today())


@app.get("/coverage", dependencies=[Depends(require_read)], tags=["model"])
def get_coverage() -> dict[str, Any]:
    with tx() as conn:
        return intel.coverage(conn)


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _rebuild() -> dict[str, Any]:
    try:
        with tx() as conn:
            return {"ok": True, **model_repo.rebuild(conn, registers.all_registers())}
    except Exception as exc:  # noqa: BLE001 — the ingest is recorded; a failed build is reported, not raised
        log.exception("model build failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _run_and_release(targets, force: bool, accept_count_delta: bool) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    try:
        results = engine.ingest_many(targets, make_store(), force=force, accept_count_delta=accept_count_delta)
        outcome = {"ok": True, "results": [r.as_dict() for r in results],
                   "complete": sum(r.complete for r in results),
                   "incomplete": [r.slug for r in results if not r.complete and not r.skipped],
                   "skipped": [r.slug for r in results if r.skipped]}
        # The model follows any batch that changed what the registers hold.
        if any(r.complete for r in results):
            outcome["model"] = _rebuild()
    except Exception as exc:  # noqa: BLE001 — a background task has nobody to raise to
        log.exception("ingest batch failed")
        outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _running.release()
    _last.clear()
    _last.update({"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), **outcome})
    return dict(_last)
