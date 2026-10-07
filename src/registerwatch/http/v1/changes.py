"""The change feed: every row added to or removed from a set of registers,
oldest first, cursor-paged and never truncated (F4). Mounted under a register
and under a jurisdiction; this module holds what both share."""

from __future__ import annotations

from typing import Any

from fastapi import Query, Request, Response

from registerwatch import query
from registerwatch.http import cursor, paging
from registerwatch.http.deps import Timestamp, connection
from registerwatch.http.models import ChangeEvent, ChangePage
from registerwatch.http.problems import invalid
from registerwatch.registers.base import Register

SINCE: Any = Query(None, description="Only changes recorded at or after this RFC 3339 time, with an offset "
                                     "(`2026-10-01T00:00:00Z`)")
UNTIL: Any = Query(None, description="Only changes recorded before this time; after `since`")


def feed(request: Request, response: Response, regs: list[Register], over: tuple[str, str],
         since: Timestamp | None, until: Timestamp | None, limit: int, token: str | None) -> ChangePage:
    if since and until and until <= since:
        raise invalid("until", "must be after since")
    scope = cursor.scope_of("changes", *over, since, until)
    at = paging.position(token, scope)
    after = _key(at["k"]) if at is not None else None
    with connection() as conn:
        events = query.change_feed(conn, regs, since=since, until=until, after=after, limit=limit)
    last = list(events[limit - 1]["key"]) if len(events) > limit else None
    pagination = paging.page(request, response, events, limit, scope, None if last is None else {"k": last})
    return ChangePage(data=[ChangeEvent.of(e) for e in events], pagination=pagination)


def _key(raw: Any) -> query.FeedKey:
    ok = (isinstance(raw, list) and len(raw) == 5 and all(isinstance(raw[i], int) and not isinstance(raw[i], bool)
                                                          for i in (0, 3))
          and all(isinstance(raw[i], str) for i in (1, 2, 4)) and raw[4] in ("added", "removed")
          and 0 <= raw[0] < 2**63 and 0 <= raw[3] < 2**63)
    if not ok:
        raise paging.bad_cursor()
    return raw[0], raw[1], raw[2], raw[3], raw[4]
