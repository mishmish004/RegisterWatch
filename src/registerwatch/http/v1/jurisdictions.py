"""Jurisdictions: the registers grouped the way people ask about them."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from registerwatch import jurisdictions
from registerwatch.http.deps import freshness, jurisdiction_or_404, known_parameters_only, require_read
from registerwatch.http.models import Jurisdiction, JurisdictionDetail, JurisdictionPage, Pagination
from registerwatch.http.problems import responses

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
