"""What every paged v1 collection shares: the `limit` and `cursor` parameters,
the cursor's binding to its query, and the RFC 8288 `Link: rel="next"` header."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import Query, Request, Response

from registerwatch import query
from registerwatch.http import cursor
from registerwatch.http.models import Pagination
from registerwatch.http.problems import Catalog, ProblemError

LIMIT: Any = Query(100, ge=1, le=query.MAX_LIMIT, description="Page size")
CURSOR: Any = Query(None, alias="cursor", description="`next_cursor` from the previous page")


def position(token: str | None, scope: str) -> dict[str, Any] | None:
    """The decoded cursor, or None on the first page; a 400 for anything else."""
    if not token:
        return None
    try:
        return cursor.decode(token, scope)
    except cursor.InvalidCursor as exc:
        raise bad_cursor(str(exc)) from None


def bad_cursor(why: str = "cursor is not one this API issued") -> ProblemError:
    return ProblemError(Catalog.INVALID_CURSOR, why, errors=[{"field": "cursor", "location": "query", "message": why}])


def page(request: Request, response: Response, items: list[Any], limit: int, scope: str,
         next_position: dict[str, Any] | None, total: int | None = None) -> Pagination:
    """`items` holds up to limit + 1; the extra one only says there is a next page."""
    has_more = len(items) > limit
    del items[limit:]
    token = cursor.encode(next_position, scope) if has_more and next_position is not None else None
    if token:
        params = [(k, v) for k, v in request.query_params.multi_items() if k != "cursor"] + [("cursor", token)]
        response.headers["Link"] = f'<{request.url.path}?{urlencode(params)}>; rel="next"'
    return Pagination(next_cursor=token, has_more=has_more, limit=limit, total=total)
