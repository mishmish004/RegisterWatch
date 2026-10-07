"""Requests no route should see.

Postgres text cannot hold a NUL byte, so a parameter carrying one would reach
the database only to fail there, as a 500 (F18). Every path, legacy included,
refuses it up front with a 400 that names the parameter: a problem under /v1,
FastAPI's `{"detail": ...}` shape elsewhere.

No request needs a body over MAX_BODY_BYTES (an ingest run's is a few hundred
bytes), and nothing else bounds one (plan.md P10.4). A body declared larger is a
413 before anything reads it: the client that waits for `100 Continue` never
sends it, and the answer closes the connection rather than read the rest. A
body that turns out larger as it arrives (chunked, no length) is a 413 at the
byte that crosses the line.

Plain ASGI middlewares, like the request id, so background tasks are untouched.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from registerwatch.http import problems

MAX_BODY_BYTES = 64 * 1024
_CLOSE = {"Connection": "close"}


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


class BodyLimitMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = next((v for k, v in scope.get("headers", []) if k == b"content-length"), None)
        if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await too_large(Request(scope))(scope, receive, send)
            return
        seen = 0

        async def counted() -> dict:
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > MAX_BODY_BYTES:
                # An HTTPException, so FastAPI passes it on rather than make it a parse error.
                raise HTTPException(413, too_large_detail(), headers=_CLOSE)
            return message

        await self.app(scope, counted, send)


def too_large_detail() -> str:
    return f"the request body is over {MAX_BODY_BYTES} bytes; no operation takes one that large"


def too_large(request: Request) -> JSONResponse:
    if problems.is_v1(request):
        return problems.response(problems.Catalog.CONTENT_TOO_LARGE, too_large_detail(), request, headers=_CLOSE)
    return JSONResponse({"detail": too_large_detail()}, status_code=413, headers=_CLOSE)
