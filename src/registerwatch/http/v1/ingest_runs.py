"""Ingest runs: start a batch, then watch it as a resource (F3, F8).

`POST /v1/ingest-runs` answers 202 with the run and runs it after the response;
the run's row is the record, so any replica (or the same one after a restart)
can answer `GET /v1/ingest-runs/{id}`. `?wait` is not carried over from legacy.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, Path, Request, Response

from registerwatch.db.repos import ingest_runs as repo
from registerwatch.http import cursor, paging, request_id
from registerwatch.http.deps import (
    connection,
    json_body_only,
    jurisdiction_or_404,
    known_parameters_only,
    register_or_404,
    require_ingest,
)
from registerwatch.http.models import IngestRun, IngestRunPage, IngestRunRequest
from registerwatch.http.problems import Catalog, ProblemError, invalid, responses
from registerwatch.ingest import runs
from registerwatch.registers import all_registers
from registerwatch.registers.base import Register

router = APIRouter(dependencies=[Depends(require_ingest), Depends(known_parameters_only)])
READ_ERRORS = responses(400, 401, 403, 429, 500, 503)
RETRY_AFTER_BUSY = "60"


@router.post("/ingest-runs", status_code=202, operation_id="createIngestRun", tags=["ingest"],
             summary="Start an ingest run",
             description="Answers at once with the run; poll its `url` (also in `Location`). One run at a "
                         "time across every replica: a second is a 409 linking the active one. With "
                         "`Idempotency-Key`, a repeat of the same request within 24 hours returns the "
                         "original run (200) instead of starting another.",
             responses={200: {"model": IngestRun, "description": "A replay of an earlier request with the same "
                                                                 "`Idempotency-Key`: the original run"},
                        **responses(400, 401, 403, 404, 409, 415, 422, 429, 500, 503)},
             dependencies=[Depends(json_body_only)])
def create_ingest_run(
    response: Response,
    background: BackgroundTasks,
    body: IngestRunRequest | None = Body(None),
    idempotency_key: str | None = Header(None, min_length=1, max_length=255,
                                         description="Any string you choose, unique per intended run"),
) -> IngestRun:
    req = body or IngestRunRequest()
    targets = _targets(req)
    requested = req.model_dump()
    digest = runs.request_hash(requested)
    if idempotency_key:
        with connection() as conn:
            earlier = repo.by_key(conn, idempotency_key)
        if earlier is not None:
            if bytes(earlier["request_hash"]) != digest:
                raise ProblemError(Catalog.IDEMPOTENCY_KEY_REUSED,
                                   f"Idempotency-Key {idempotency_key!r} was used with a different body",
                                   original_run=f"/v1/ingest-runs/{earlier['id']}")
            response.status_code = 200
            return IngestRun.of(earlier)
    lease = runs.acquire()
    if lease is None:
        with connection() as conn:
            active = repo.active(conn)
        url = f"/v1/ingest-runs/{active['id']}" if active else None
        raise ProblemError(Catalog.INGEST_IN_PROGRESS,
                           f"an ingest run is already going: {url}" if url else "an ingest is already going",
                           headers={"Retry-After": RETRY_AFTER_BUSY}, active_run=url)
    try:
        run_id = uuid.UUID(request_id.uuid7())
        with connection() as conn:
            run = repo.create(conn, run_id, requested, [r.slug for r in targets], idempotency_key,
                              digest if idempotency_key else None)
    except BaseException:
        lease.release()
        raise
    background.add_task(runs.execute, lease, run_id, targets, force=req.force,
                        accept_count_delta=req.accept_count_delta)
    response.headers["Location"] = f"/v1/ingest-runs/{run_id}"
    return IngestRun.of(run)


@router.get("/ingest-runs", operation_id="listIngestRuns", tags=["ingest"], summary="List ingest runs, newest first",
            responses=READ_ERRORS)
def list_ingest_runs(request: Request, response: Response, limit: int = paging.LIMIT,
                     cursor_: str | None = paging.CURSOR) -> IngestRunPage:
    scope = cursor.scope_of("ingest-runs")
    at = paging.position(cursor_, scope)
    before = _before(at) if at is not None else None
    with connection() as conn:
        rows = repo.page(conn, before=before, limit=limit)
    last = rows[limit - 1] if len(rows) > limit else None
    pagination = paging.page(request, response, rows, limit, scope,
                             None if last is None else {"c": last["created_at"].isoformat(), "i": str(last["id"])})
    return IngestRunPage(data=[IngestRun.of(r) for r in rows], pagination=pagination)


@router.get("/ingest-runs/{id}", operation_id="getIngestRun", tags=["ingest"], summary="Get one ingest run",
            responses={**READ_ERRORS, **responses(404)})
def get_ingest_run(id: uuid.UUID = Path(description="The run's id, from its `url`")) -> IngestRun:
    with connection() as conn:
        run = repo.get(conn, id)
    if run is None:
        raise ProblemError(Catalog.INGEST_RUN_NOT_FOUND, f"no ingest run {id}")
    return IngestRun.of(run)


def _targets(req: IngestRunRequest) -> list[Register]:
    if req.registers is not None and req.jurisdiction is not None:
        raise invalid("registers", "give `registers` or `jurisdiction`, not both", location="body")
    if req.registers is not None:
        seen: dict[str, Register] = {}
        for slug in req.registers:
            reg = register_or_404(slug)
            seen.setdefault(reg.slug, reg)
        return list(seen.values())
    if req.jurisdiction is not None:
        return jurisdiction_or_404(req.jurisdiction)
    return all_registers()


def _before(at: dict) -> tuple[datetime, uuid.UUID]:
    try:
        return datetime.fromisoformat(at["c"]), uuid.UUID(at["i"])
    except (KeyError, TypeError, ValueError):
        raise paging.bad_cursor() from None
