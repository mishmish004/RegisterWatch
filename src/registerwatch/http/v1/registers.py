"""Registers, their tables, and the tables' current rows."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Path, Query, Request

from registerwatch import query, registers
from registerwatch.http import cursor
from registerwatch.http.deps import (
    connection,
    freshness,
    known_parameters_only,
    register_or_404,
    require_read,
    table_or_404,
)
from registerwatch.http.models import (
    Pagination,
    Register,
    RegisterDetail,
    RegisterPage,
    Row,
    RowPage,
    TableSchema,
)
from registerwatch.http.problems import Catalog, ProblemError, invalid, responses

router = APIRouter(dependencies=[Depends(require_read), Depends(known_parameters_only)])
ERRORS = responses(400, 401, 429, 500, 503)

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
            summary="Get a register with its tables and freshness", responses={**ERRORS, **responses(404)})
def get_register(slug: str = SLUG) -> RegisterDetail:
    return RegisterDetail.with_health(register_or_404(slug), freshness())


@router.get("/registers/{slug}/tables/{table}", operation_id="getTable", tags=["registers"],
            summary="Get a table's columns", responses={**ERRORS, **responses(404)})
def get_table(slug: str = SLUG, table: str = TABLE) -> TableSchema:
    reg = register_or_404(slug)
    return TableSchema.of(reg, table_or_404(reg, table))


@router.get("/registers/{slug}/tables/{table}/rows", operation_id="listRows", tags=["rows"],
            summary="List a table's current rows", responses={**ERRORS, **responses(404)},
            openapi_extra={"parameters": [FILTER_PARAMETER]})
def list_rows(
    request: Request,
    slug: str = SLUG,
    table: str = TABLE,
    q: str | None = Query(None, min_length=2, max_length=200,
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
        if offset < 0:
            raise ValueError
    except (cursor.InvalidCursor, KeyError, TypeError, ValueError) as exc:
        why = str(exc) if isinstance(exc, cursor.InvalidCursor) else "cursor is not one this API issued"
        raise ProblemError(Catalog.INVALID_CURSOR, why,
                           errors=[{"field": "cursor", "location": "query", "message": why}]) from None
    with connection() as conn:
        res = query.rows(conn, reg, tbl, q=q, filters=filters, limit=limit, offset=offset)
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
