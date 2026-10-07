"""Text search across every register's current rows."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query

from registerwatch import query
from registerwatch.http.deps import (
    JURISDICTION,
    connection,
    known_parameters_only,
    registers_for,
    require_read,
    table_of,
)
from registerwatch.http.models import Pagination, Row, SearchHit, SearchHitPage
from registerwatch.http.problems import responses

router = APIRouter(dependencies=[Depends(require_read), Depends(known_parameters_only)])


@router.get("/search", operation_id="search", tags=["search"],
            summary="Find text in every register's current rows", responses=responses(400, 401, 429, 500, 503))
def search(
    q: str = Query(..., min_length=2, max_length=200, description="Case-insensitive substring"),
    jurisdiction: list[str] | None = JURISDICTION,
    limit: int = Query(20, ge=1, le=100, description="Rows returned per table"),
) -> SearchHitPage:
    regs = registers_for(jurisdiction)
    with connection() as conn:
        hits = query.search_tables(conn, q, regs, limit=limit)
    data = [SearchHit(
        jurisdiction=h["jurisdiction"], register_=h["register"], regulator=h["regulator"], kind=h["kind"],
        table=h["table"], total=h["total"],
        rows=[Row.of(table_of(h["register"], h["table"]), r) for r in h["rows"]],
        rows_url=f"/v1/registers/{h['register']}/tables/{h['table']}/rows?"
                 + urlencode({"q": q, "include_total": "true"}),
    ) for h in hits]
    return SearchHitPage(data=data, pagination=Pagination.whole(data))
