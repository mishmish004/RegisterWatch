"""Domains: where a website is licensed, and where it is blocked."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request, Response

from registerwatch import query
from registerwatch.http.caching import conditional
from registerwatch.http.deps import (
    JURISDICTION,
    connection,
    known_parameters_only,
    registers_for,
    require_read,
    table_of,
)
from registerwatch.http.models import DomainMatch, DomainStatus, Row
from registerwatch.http.problems import invalid, responses
from registerwatch.registers.extract import host

router = APIRouter(dependencies=[Depends(require_read), Depends(known_parameters_only)])


@router.get("/domains/{domain}", operation_id="getDomainStatus", tags=["domains"],
            summary="Where a domain is licensed, and where it is blocked",
            description="A leading `www.` is ignored, and a listed subdomain counts as a match of its "
                        "parent (`nj.bet365.com` for `bet365.com`), marked `subdomain`.",
            responses=responses(400, 401, 429, 500, 503, 504))
def get_domain_status(
    request: Request,
    response: Response,
    domain: str = Path(max_length=253, description="A hostname; a URL is accepted and reduced to its host",
                       examples=["bet365.com"]),
    jurisdiction: list[str] | None = JURISDICTION,
) -> DomainStatus:
    if not host(domain):
        raise invalid("domain", f"{domain!r} is not a hostname", location="path")
    regs = registers_for(jurisdiction)
    conditional(request, response, regs)
    with connection() as conn:
        res = query.check_domain(conn, domain, regs)
    return DomainStatus(domain=res["domain"], licensed_in=res["licensed_in"], blocked_in=res["blocked_in"],
                        matches=[DomainMatch(jurisdiction=m["jurisdiction"], register_=m["register"],
                                             regulator=m["regulator"], kind=m["kind"], table=m["table"],
                                             match=m["match"], row=Row.of(table_of(m["register"], m["table"]),
                                                                          m["row"]))
                                 for m in res["matches"]])
