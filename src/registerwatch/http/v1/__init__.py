"""API v1, mounted at /v1. One router per resource; plan.md §2.2 lists the target set."""

from __future__ import annotations

from fastapi import APIRouter

from registerwatch.http.v1 import domains, jurisdictions, registers, search

router = APIRouter(prefix="/v1")
for _module in (jurisdictions, registers, search, domains):
    router.include_router(_module.router)
