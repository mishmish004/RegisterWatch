"""Jurisdictions: the registers grouped the way people ask about them."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request, Response

from registerwatch import jurisdictions
from registerwatch.http import paging
from registerwatch.http.deps import (
    Timestamp,
    freshness,
    jurisdiction_or_404,
    known_parameters_only,
    require_read,
)
from registerwatch.http.models import ChangePage, Jurisdiction, JurisdictionDetail, JurisdictionPage, Pagination
from registerwatch.http.problems import responses
from registerwatch.http.v1.changes import SINCE, UNTIL, feed

router = APIRouter(dependencies=[Depends(require_read), Depends(known_parameters_only)])
ERRORS = responses(400, 401, 429, 500, 503)

CODE = Path(description="Jurisdiction code, any case, `-` or `_`: `gb`, `US-NJ`, `ca_on`", examples=["ch"])


@router.get("/jurisdictions", operation_id="listJurisdictions", tags=["jurisdictions"],
            summary="List jurisdictions", responses=ERRORS)
def list_jurisdictions() -> JurisdictionPage:
    data = [Jurisdiction.of(code, regs) for code, regs in jurisdictions.by_code().items()]
    return JurisdictionPage(data=data, pagination=Pagination.whole(data))


@router.get("/jurisdictions/{code}", operation_id="getJurisdiction", tags=["jurisdictions"],
            summary="Get a jurisdiction with its registers and their freshness",
            responses={**ERRORS, **responses(404)})
def get_jurisdiction(code: str = CODE) -> JurisdictionDetail:
    regs = jurisdiction_or_404(code)
    return JurisdictionDetail.of(code, regs, freshness())


@router.get("/jurisdictions/{code}/changes", operation_id="listJurisdictionChanges", tags=["changes"],
            summary="Rows added and removed across a jurisdiction's registers, oldest first",
            description="Registers interleave by when their snapshots were recorded. Each register's first "
                        "complete snapshot is its baseline, not a change.",
            responses={**ERRORS, **responses(404)})
def list_jurisdiction_changes(request: Request, response: Response, code: str = CODE,
                              since: Timestamp | None = SINCE, until: Timestamp | None = UNTIL,
                              limit: int = paging.LIMIT, cursor_: str | None = paging.CURSOR) -> ChangePage:
    regs = jurisdiction_or_404(code)
    return feed(request, response, regs, ("jurisdiction", jurisdictions.normalise(code)), since, until, limit,
                cursor_)
