"""Registers, their tables, the tables' rows, their changes and their snapshots."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from registerwatch import query, registers
from registerwatch.http import cursor, paging
from registerwatch.http.deps import (
    Timestamp,
    connection,
    freshness,
    known_parameters_only,
    register_or_404,
    require_read,
    table_or_404,
)
from registerwatch.http.models import (
    ChangePage,
    Pagination,
    Register,
    RegisterDetail,
    RegisterPage,
    Row,
    RowDetail,
    RowPage,
    Snapshot,
    SnapshotPage,
    TableSchema,
)
from registerwatch.http.problems import Catalog, ProblemError, invalid, responses
from registerwatch.http.v1.changes import SINCE, UNTIL, feed

router = APIRouter(dependencies=[Depends(require_read), Depends(known_parameters_only)])
ERRORS = responses(400, 401, 429, 500, 503)
WITH_404 = {**ERRORS, **responses(404)}

SLUG = Path(description="Register slug, e.g. `gb_ukgc`", examples=["gb_ukgc"])
TABLE = Path(description="Table name within the register", examples=["licences"])

# `filter[<column>]=<value>`: an exact match on one of the table's columns.
# Column filters get their own namespace so no column name can ever collide
# with a parameter of the endpoint itself.
_FILTER = re.compile(r"^filter\[([a-z][a-z0-9_]*)\]$")
_ROW_PARAMS = {"q", "limit", "cursor", "include_total"}
FILTER_PARAMETER = {
    "name": "filter", "in": "query", "style": "deepObject", "explode": True, "required": False,
    "description": "Exact match on a column, e.g. `filter[status]=Active&filter[type]=Remote`. "
                   "Columns are listed by getTable; any other column is a 400.",
    "schema": {"type": "object", "additionalProperties": {"type": "string"}},
}


@router.get("/registers", operation_id="listRegisters", tags=["registers"], summary="List registers",
            responses=ERRORS)
def list_registers() -> RegisterPage:
    data = [Register.of(r) for r in registers.all_registers()]
    return RegisterPage(data=data, pagination=Pagination.whole(data))


@router.get("/registers/{slug}", operation_id="getRegister", tags=["registers"],
            summary="Get a register with its tables and freshness", responses=WITH_404)
def get_register(slug: str = SLUG) -> RegisterDetail:
    return RegisterDetail.with_health(register_or_404(slug), freshness())


@router.get("/registers/{slug}/tables/{table}", operation_id="getTable", tags=["registers"],
            summary="Get a table's columns", responses=WITH_404)
def get_table(slug: str = SLUG, table: str = TABLE) -> TableSchema:
    reg = register_or_404(slug)
    return TableSchema.of(reg, table_or_404(reg, table))


@router.get("/registers/{slug}/tables/{table}/rows", operation_id="listRows", tags=["rows"],
            summary="List a table's current rows",
            description="Rows in `id` order. Every page of one walk is read as of the snapshot the first "
                        "page was, so an ingest during the walk neither repeats nor skips a row.",
            responses=WITH_404, openapi_extra={"parameters": [FILTER_PARAMETER]})
def list_rows(
    request: Request,
    response: Response,
    slug: str = SLUG,
    table: str = TABLE,
    q: str | None = Query(None, min_length=2, max_length=200,
                          description="Case-insensitive substring in any text column"),
    limit: int = paging.LIMIT,
    cursor_: str | None = paging.CURSOR,
    include_total: bool = Query(False, description="Also count every matching row (slower)"),
) -> RowPage:
    reg = register_or_404(slug)
    tbl = table_or_404(reg, table)
    filters = _filters(request, tbl.column_names)
    scope = cursor.scope_of("rows", reg.slug, tbl.name, q, sorted(filters.items()))
    at = paging.position(cursor_, scope)
    with connection() as conn:
        latest = query.latest_snapshot_id(conn, reg)
        snapshot, after = (latest, 0) if at is None else _keyed(at)
        rows = query.row_page(conn, reg, tbl, snapshot_id=snapshot, latest=latest, after=after, limit=limit,
                              q=q, filters=filters)
        total = (query.count_rows(conn, reg, tbl, snapshot_id=snapshot, latest=latest, q=q, filters=filters)
                 if include_total else None)
    last = rows[limit - 1]["id"] if len(rows) > limit else None
    pagination = paging.page(request, response, rows, limit, scope,
                             None if last is None else {"k": last, "at": snapshot}, total)
    return RowPage(data=[Row.of(tbl, r) for r in rows], pagination=pagination)


@router.get("/registers/{slug}/tables/{table}/rows/{id}", operation_id="getRow", tags=["rows"],
            summary="Get one row, current or not, with its history", responses=WITH_404)
def get_row(slug: str = SLUG, table: str = TABLE,
            id: int = Path(ge=1, le=2**63 - 1, description="The row's `id`", examples=[1187])) -> RowDetail:
    reg = register_or_404(slug)
    tbl = table_or_404(reg, table)
    with connection() as conn:
        row = query.get_row(conn, reg, tbl, id)
    if row is None:
        raise ProblemError(Catalog.ROW_NOT_FOUND, f"no row {id} in {reg.slug}.{tbl.name}")
    return RowDetail.of(tbl, row, register=reg.slug)


@router.get("/registers/{slug}/changes", operation_id="listRegisterChanges", tags=["changes"],
            summary="Rows added and removed in a register, oldest first",
            description="The register's first complete snapshot is its baseline, not a change. Never "
                        "truncated: follow `next_cursor` until `has_more` is false.",
            responses=WITH_404)
def list_register_changes(request: Request, response: Response, slug: str = SLUG,
                          since: Timestamp | None = SINCE, until: Timestamp | None = UNTIL,
                          limit: int = paging.LIMIT, cursor_: str | None = paging.CURSOR) -> ChangePage:
    reg = register_or_404(slug)
    return feed(request, response, [reg], ("register", reg.slug), since, until, limit, cursor_)


@router.get("/registers/{slug}/snapshots", operation_id="listSnapshots", tags=["registers"],
            summary="Every ingest run of a register, complete or not, newest first", responses=WITH_404)
def list_snapshots(request: Request, response: Response, slug: str = SLUG, limit: int = paging.LIMIT,
                   cursor_: str | None = paging.CURSOR) -> SnapshotPage:
    reg = register_or_404(slug)
    scope = cursor.scope_of("snapshots", reg.slug)
    at = paging.position(cursor_, scope)
    before = _int(at, "k") if at is not None else None
    with connection() as conn:
        rows = query.snapshot_page(conn, reg, before=before, limit=limit)
    last = rows[limit - 1]["id"] if len(rows) > limit else None
    pagination = paging.page(request, response, rows, limit, scope, None if last is None else {"k": last})
    return SnapshotPage(data=[Snapshot(register_=reg.slug, **r) for r in rows], pagination=pagination)


def _keyed(at: dict) -> tuple[int, int]:
    return _int(at, "at"), _int(at, "k")


def _int(at: dict, key: str) -> int:
    value = at.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**63:
        raise paging.bad_cursor()
    return value


def _filters(request: Request, columns: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key in _ROW_PARAMS:
            continue
        m = _FILTER.match(key)
        if not m:
            hint = f"; to filter on the column, use filter[{key}]=" if key in columns else ""
            raise invalid(key, f"not a parameter of this operation{hint}")
        col = m.group(1)
        if col not in columns:
            raise ProblemError(Catalog.UNKNOWN_FILTER_COLUMN, f"no column {col!r} to filter on; "
                               f"columns: {', '.join(columns)}",
                               errors=[{"field": key, "location": "query", "message": "unknown column"}])
        if col in out:
            raise invalid(key, "given more than once")
        out[col] = value
    return out

