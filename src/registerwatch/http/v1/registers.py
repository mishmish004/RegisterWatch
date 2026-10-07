"""Registers, their tables, and the tables' current rows."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from registerwatch import query, registers
from registerwatch.http import cursor
from registerwatch.http.deps import connection, freshness, register_or_404, require_read, table_or_404
from registerwatch.http.models import (
    BAD_REQUEST,
    NOT_FOUND,
    Pagination,
    Register,
    RegisterDetail,
    RegisterPage,
    Row,
    RowPage,
    TableSchema,
)

router = APIRouter(dependencies=[Depends(require_read)])

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


@router.get("/registers", operation_id="listRegisters", tags=["registers"], summary="List registers")
def list_registers() -> RegisterPage:
    data = [Register.of(r) for r in registers.all_registers()]
    return RegisterPage(data=data, pagination=Pagination.whole(data))


@router.get("/registers/{slug}", operation_id="getRegister", tags=["registers"],
            summary="Get a register with its tables and freshness", responses=NOT_FOUND)
def get_register(slug: str = SLUG) -> RegisterDetail:
    return RegisterDetail.with_health(register_or_404(slug), freshness())


@router.get("/registers/{slug}/tables/{table}", operation_id="getTable", tags=["registers"],
            summary="Get a table's columns", responses=NOT_FOUND)
def get_table(slug: str = SLUG, table: str = TABLE) -> TableSchema:
    reg = register_or_404(slug)
    return TableSchema.of(reg, table_or_404(reg, table))


@router.get("/registers/{slug}/tables/{table}/rows", operation_id="listRows", tags=["rows"],
            summary="List a table's current rows", responses={**BAD_REQUEST, **NOT_FOUND},
            openapi_extra={"parameters": [FILTER_PARAMETER]})
def list_rows(
    request: Request,
    slug: str = SLUG,
    table: str = TABLE,
    q: str | None = Query(None, min_length=1, max_length=200,
                          description="Case-insensitive substring in any text column"),
    limit: int = Query(100, ge=1, le=query.MAX_LIMIT),
    cursor_: str | None = Query(None, alias="cursor", description="`next_cursor` from the previous page"),
    include_total: bool = Query(False, description="Also count every matching row (slower)"),
) -> RowPage:
    reg = register_or_404(slug)
    tbl = table_or_404(reg, table)
    filters = _filters(request, tbl.column_names)
    scope = cursor.scope_of(reg.slug, tbl.name, q, sorted(filters.items()))
    try:
        offset = int(cursor.decode(cursor_, scope)["o"]) if cursor_ else 0
    except (cursor.InvalidCursor, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"invalid cursor: {exc}") from None
    if offset < 0:
        raise HTTPException(400, "invalid cursor: cursor is not one this API issued")
    try:
        with connection() as conn:
            res = query.rows(conn, reg, tbl, q=q, filters=filters, limit=limit, offset=offset)
    except ValueError as exc:  # a filter on a column the table does not declare
        raise HTTPException(400, str(exc)) from None
    data = [Row.of(tbl, r) for r in res["rows"]]
    has_more = offset + len(data) < res["total"]
    return RowPage(data=data, pagination=Pagination(
        next_cursor=cursor.encode({"o": offset + len(data)}, scope) if has_more else None,
        has_more=has_more, limit=limit, total=res["total"] if include_total else None))


def _filters(request: Request, columns: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key in _ROW_PARAMS:
            continue
        m = _FILTER.match(key)
        if not m:
            hint = f"; to filter on the column, use filter[{key}]=" if key in columns else ""
            raise HTTPException(400, f"unknown query parameter {key!r}{hint}")
        if m.group(1) in out:
            raise HTTPException(400, f"filter[{m.group(1)}] given more than once")
        out[m.group(1)] = value
    return out
