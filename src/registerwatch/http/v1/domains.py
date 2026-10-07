"""Domains: where a website is licensed, and where it is blocked."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path

from registerwatch import query
from registerwatch.http.deps import JURISDICTION, connection, registers_for, require_read, table_of
from registerwatch.http.models import BAD_REQUEST, NOT_FOUND, DomainMatch, DomainStatus, Row

router = APIRouter(dependencies=[Depends(require_read)])


@router.get("/domains/{domain}", operation_id="getDomainStatus", tags=["domains"],
            summary="Where a domain is licensed, and where it is blocked",
            description="A leading `www.` is ignored, and a listed subdomain counts as a match of its "
                        "parent (`nj.bet365.com` for `bet365.com`), marked `subdomain`.",
            responses={**BAD_REQUEST, **NOT_FOUND})
def get_domain_status(
    domain: str = Path(description="A hostname; a URL is accepted and reduced to its host",
                       examples=["bet365.com"]),
    jurisdiction: list[str] | None = JURISDICTION,
) -> DomainStatus:
    regs = registers_for(jurisdiction)
    try:
        with connection() as conn:
            res = query.check_domain(conn, domain, regs)
    except ValueError as exc:  # not a hostname
        raise HTTPException(400, str(exc)) from None
    return DomainStatus(domain=res["domain"], licensed_in=res["licensed_in"], blocked_in=res["blocked_in"],
                        matches=[DomainMatch(jurisdiction=m["jurisdiction"], register_=m["register"],
                                             regulator=m["regulator"], kind=m["kind"], table=m["table"],
                                             match=m["match"], row=Row.of(table_of(m["register"], m["table"]),
                                                                          m["row"]))
                                 for m in res["matches"]])
