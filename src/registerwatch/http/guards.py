"""Requests no route should see.

Postgres text cannot hold a NUL byte, so a parameter carrying one would reach
the database only to fail there, as a 500 (F18). Every path, legacy included,
refuses it up front with a 400 that names the parameter: a problem under /v1,
FastAPI's `{"detail": ...}` shape elsewhere.

A plain ASGI middleware, like the request id, so background tasks are untouched.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.responses import JSONResponse

from registerwatch.http import problems


def nul_field(scope: dict) -> tuple[str, str] | None:
    """(location, field) of the first value carrying a NUL byte, if any."""
    if "\x00" in scope.get("path", ""):
        return "path", "path"
    raw = scope.get("query_string", b"").decode("latin-1")
    for key, value in parse_qsl(raw, keep_blank_values=True, encoding="utf-8", errors="replace"):
        if "\x00" in key or "\x00" in value:
            return "query", key.replace("\x00", "\\0")
    return None


class NulGuardMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        bad = nul_field(scope) if scope["type"] == "http" else None
        if bad is None:
            await self.app(scope, receive, send)
            return
        location, field = bad
        request = Request(scope)
        message = "contains a NUL byte, which no value can hold"
        if problems.is_v1(request):
            response = problems.invalid(field, message, location=location)
            answer = problems.response(response.problem, response.detail, request, errors=response.errors)
        else:
            answer = JSONResponse({"detail": f"{field}: {message}"}, status_code=400)
        await answer(scope, receive, send)
