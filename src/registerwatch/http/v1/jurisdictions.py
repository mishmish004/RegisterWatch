"""Jurisdictions: the registers grouped the way people ask about them."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from registerwatch import jurisdictions
from registerwatch.http.deps import freshness, jurisdiction_or_404, require_read
from registerwatch.http.models import NOT_FOUND, Jurisdiction, JurisdictionDetail, JurisdictionPage, Pagination

router = APIRouter(dependencies=[Depends(require_read)])

CODE = Path(description="Jurisdiction code, any case, `-` or `_`: `gb`, `US-NJ`, `ca_on`", examples=["ch"])


@router.get("/jurisdictions", operation_id="listJurisdictions", tags=["jurisdictions"],
            summary="List jurisdictions")
def list_jurisdictions() -> JurisdictionPage:
    data = [Jurisdiction.of(code, regs) for code, regs in jurisdictions.by_code().items()]
    return JurisdictionPage(data=data, pagination=Pagination.whole(data))


@router.get("/jurisdictions/{code}", operation_id="getJurisdiction", tags=["jurisdictions"],
            summary="Get a jurisdiction with its registers and their freshness", responses=NOT_FOUND)
def get_jurisdiction(code: str = CODE) -> JurisdictionDetail:
    regs = jurisdiction_or_404(code)
    return JurisdictionDetail.of(code, regs, freshness())
