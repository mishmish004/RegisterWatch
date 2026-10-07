"""How fresh every register is (F13).

Always a 200, so a status page or a load balancer never mistakes stale data for
a broken server. `?strict=true` turns staleness into a 503 for uptime monitors,
which only read the status code: the old `/status` behaviour, asked for by name.
Open, like the probes: freshness is not register data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response

from registerwatch import jurisdictions, registers
from registerwatch.http import deps
from registerwatch.http.deps import freshness, known_parameters_only
from registerwatch.http.models import Freshness, RegisterStatus, Status
from registerwatch.http.problems import responses

router = APIRouter(dependencies=[Depends(known_parameters_only)])


@router.get("/status", operation_id="getStatus", tags=["operations"], summary="Freshness of every register",
            description="Always 200 unless `strict=true`. A register is stale once its newest complete snapshot "
                        "is older than `stale_after_h` hours, or when the database cannot be reached.",
            responses={503: {"model": Status, "description": "`strict=true` and something is stale: the same "
                                                             "document, with a status code a monitor can alarm on"},
                       **responses(400, 429, 500)},
            openapi_extra={"security": []})
def get_status(response: Response, strict: bool = Query(False, description="Answer 503 when anything is stale, "
                                                                           "for uptime monitors")) -> Status:
    health = freshness()
    now = datetime.now(timezone.utc)
    after = deps.settings().stale_after_h
    out = []
    for reg in registers.all_registers():
        h = None if health is None else health.get(reg.slug, {})
        last_good = (h or {}).get("last_good")
        age = None if last_good is None else (now - last_good).total_seconds() / 3600
        out.append(RegisterStatus(slug=reg.slug, jurisdiction=jurisdictions.normalise(reg.country),
                                  stale=age is None or age > after,
                                  hours_since_good=None if age is None else round(age, 1),
                                  freshness=None if h is None else Freshness.of(h), url=f"/v1/registers/{reg.slug}"))
    stale = [r.slug for r in out if r.stale]
    status = Status(stale=bool(stale) or health is None, database="ok" if health is not None else "unreachable",
                    stale_after_h=after, stale_registers=stale, checked_at=now, registers=out)
    if strict and status.stale:
        response.status_code = 503
    return status
