"""Every error v1 can answer with, as RFC 9457 Problem Details.

`Catalog` is the single list. Each entry has a stable `type` URI pointing at
its section of docs/problems.md, which says what it means and what to do about
it. Routes raise `ProblemError`; the handlers installed by `install` turn it,
and every other failure under /v1, into `application/problem+json`. The spec's
`components.responses` are generated from the same list (see http/openapi.py).

Legacy routes keep FastAPI's `{"detail": ...}` bodies until they are removed,
so the handlers only reformat requests under /v1.
"""

from __future__ import annotations

import enum
import logging
from typing import Any
from urllib.parse import quote

import psycopg
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from registerwatch.http import request_id

log = logging.getLogger(__name__)

MEDIA_TYPE = "application/problem+json"
DOCS = "https://github.com/mishmish004/RegisterWatch/blob/main/docs/problems.md"


class Catalog(enum.Enum):
    # value: (status, title)
    INVALID_PARAMETER = (400, "Invalid parameter")
    UNKNOWN_FILTER_COLUMN = (400, "Unknown filter column")
    INVALID_CURSOR = (400, "Invalid cursor")
    UNAUTHENTICATED = (401, "Authentication required")
    FORBIDDEN = (403, "Not allowed with this token")
    NOT_FOUND = (404, "No such endpoint")
    JURISDICTION_NOT_FOUND = (404, "Unknown jurisdiction")
    REGISTER_NOT_FOUND = (404, "Unknown register")
    TABLE_NOT_FOUND = (404, "Unknown table")
    ROW_NOT_FOUND = (404, "Unknown row")
    INGEST_RUN_NOT_FOUND = (404, "Unknown ingest run")
    METHOD_NOT_ALLOWED = (405, "Method not allowed")
    INGEST_IN_PROGRESS = (409, "An ingest is already running")
    UNSUPPORTED_MEDIA_TYPE = (415, "Unsupported media type")
    IDEMPOTENCY_KEY_REUSED = (422, "Idempotency key reused with a different request")
    RATE_LIMITED = (429, "Too many requests")
    INTERNAL = (500, "Internal error")
    INGEST_DISABLED = (503, "Ingest is disabled")
    DATABASE_UNAVAILABLE = (503, "Database unavailable")
    QUERY_TIMEOUT = (504, "Query took too long")

    @property
    def status(self) -> int:
        return self.value[0]

    @property
    def title(self) -> str:
        return self.value[1]

    @property
    def slug(self) -> str:
        return self.name.lower().replace("_", "-")

    @property
    def type(self) -> str:
        return f"{DOCS}#{self.slug}"


class ProblemError(Exception):
    """Raise from a v1 route to answer with this problem."""

    def __init__(self, problem: Catalog, detail: str, *, errors: list[dict[str, str]] | None = None,
                 headers: dict[str, str] | None = None, **extensions: Any) -> None:
        super().__init__(detail)
        self.problem, self.detail, self.errors, self.headers, self.extensions = (
            problem, detail, errors, headers or {}, extensions)


def invalid(field: str, message: str, location: str = "query") -> ProblemError:
    """One bad parameter, named."""
    return ProblemError(Catalog.INVALID_PARAMETER, f"{field}: {message}",
                        errors=[{"field": field, "location": location, "message": message}])


def instance(request: Request) -> str:
    """The request path, percent-encoded again so it is a valid URI reference."""
    return quote(request.url.path, safe="/:@!$&'()*+,;=")


def body(problem: Catalog, detail: str, request: Request, *, errors: list[dict[str, str]] | None = None,
         **extensions: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": problem.type, "title": problem.title, "status": problem.status,
                           "detail": detail, "instance": instance(request), "request_id": request_id.current()}
    if errors:
        out["errors"] = errors
    return {**out, **extensions}


def response(problem: Catalog, detail: str, request: Request, *, errors: list[dict[str, str]] | None = None,
             headers: dict[str, str] | None = None, **extensions: Any) -> JSONResponse:
    hdrs = dict(headers or {})
    if problem is Catalog.UNAUTHENTICATED:
        hdrs.setdefault("WWW-Authenticate", 'Bearer realm="registerwatch"')
    if problem in (Catalog.DATABASE_UNAVAILABLE, Catalog.QUERY_TIMEOUT):
        hdrs.setdefault("Retry-After", "5")
    return JSONResponse(body(problem, detail, request, errors=errors, **extensions), status_code=problem.status,
                        headers=hdrs, media_type=MEDIA_TYPE)


def is_v1(request: Request) -> bool:
    path = request.scope.get("path", "")
    return path == "/v1" or path.startswith("/v1/")


# --- handlers ----------------------------------------------------------------------

_BY_STATUS = {400: Catalog.INVALID_PARAMETER, 401: Catalog.UNAUTHENTICATED, 403: Catalog.FORBIDDEN,
              404: Catalog.NOT_FOUND, 405: Catalog.METHOD_NOT_ALLOWED, 415: Catalog.UNSUPPORTED_MEDIA_TYPE,
              429: Catalog.RATE_LIMITED, 503: Catalog.DATABASE_UNAVAILABLE}


async def _on_problem(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return response(exc.problem, exc.detail, request, errors=exc.errors, headers=exc.headers, **exc.extensions)


async def _on_http_exception(request: Request, exc: Exception):
    assert isinstance(exc, StarletteHTTPException)
    if not is_v1(request):
        return await http_exception_handler(request, exc)
    problem = _BY_STATUS.get(exc.status_code, Catalog.INTERNAL if exc.status_code >= 500 else Catalog.INVALID_PARAMETER)
    detail = exc.detail if isinstance(exc.detail, str) else problem.title
    if problem is Catalog.NOT_FOUND:
        detail = f"no endpoint at {request.url.path}; the operations are listed in /openapi.json"
    if problem is Catalog.METHOD_NOT_ALLOWED:
        detail = f"{request.method} is not allowed on {request.url.path}"
    return response(problem, detail, request, headers=getattr(exc, "headers", None))


async def _on_validation_error(request: Request, exc: Exception):
    assert isinstance(exc, RequestValidationError)
    if not is_v1(request):
        return await request_validation_exception_handler(request, exc)
    errors = []
    for e in exc.errors():
        loc = [str(p) for p in e.get("loc", ())]
        location, field = (loc[0], ".".join(loc[1:])) if len(loc) > 1 else ("query", ".".join(loc))
        errors.append({"field": field, "location": location, "message": e.get("msg", "invalid")})
    detail = "; ".join(f"{e['field']}: {e['message']}" for e in errors) or "invalid request"
    return response(Catalog.INVALID_PARAMETER, detail, request, errors=errors)


async def _on_query_canceled(request: Request, exc: Exception):
    if not is_v1(request):
        raise exc
    return response(Catalog.QUERY_TIMEOUT, "the query was cancelled for taking too long; narrow it and retry",
                    request)


async def _on_database_error(request: Request, exc: Exception):
    if not is_v1(request):
        raise exc
    log.warning("database unavailable: %s", type(exc).__name__)
    return response(Catalog.DATABASE_UNAVAILABLE, "the database cannot be reached right now; retry shortly",
                    request)


def internal(request: Request) -> Response:
    """The answer to an unhandled error. Legacy keeps Starlette's plain text; v1
    gets a problem with nothing about the failure in it but the request id."""
    if not is_v1(request):
        return PlainTextResponse("Internal Server Error", status_code=500)
    return response(Catalog.INTERNAL, "something went wrong on our side; quote request_id if you report it",
                    request)


class UnhandledErrorMiddleware:
    """Answers an unhandled error itself, inside the request id middleware, so the
    500 carries `X-Request-Id` and the one log line for it carries the id too.
    (Starlette's own server-error handler sits outside every user middleware.)
    An error after the response started (a background task) is left to the server."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking(message: dict) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracking)
        except Exception:
            if started:
                raise
            log.exception("unhandled error")
            await internal(Request(scope))(scope, receive, send)


def install(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _on_problem)
    app.add_exception_handler(StarletteHTTPException, _on_http_exception)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(psycopg.errors.QueryCanceled, _on_query_canceled)
    app.add_exception_handler(psycopg.OperationalError, _on_database_error)


# --- the spec ------------------------------------------------------------------------

# components.responses, by status: what an operation may answer besides success.
RESPONSES = {
    400: ("BadRequest", "A parameter is invalid; `errors` names each one",
          [Catalog.INVALID_PARAMETER, Catalog.UNKNOWN_FILTER_COLUMN, Catalog.INVALID_CURSOR]),
    401: ("Unauthenticated", "A read token is configured and the request did not carry it", [Catalog.UNAUTHENTICATED]),
    403: ("Forbidden", "The token is valid but cannot do this (a read token on an ingest operation)",
          [Catalog.FORBIDDEN]),
    404: ("NotFound", "An identifier in the path or body does not exist",
          [Catalog.JURISDICTION_NOT_FOUND, Catalog.REGISTER_NOT_FOUND, Catalog.TABLE_NOT_FOUND,
           Catalog.ROW_NOT_FOUND, Catalog.INGEST_RUN_NOT_FOUND]),
    409: ("Conflict", "An ingest run is already going; `active_run` links to it", [Catalog.INGEST_IN_PROGRESS]),
    415: ("UnsupportedMediaType", "The body is not `application/json`", [Catalog.UNSUPPORTED_MEDIA_TYPE]),
    422: ("IdempotencyKeyReused", "The `Idempotency-Key` was used with a different body",
          [Catalog.IDEMPOTENCY_KEY_REUSED]),
    429: ("TooManyRequests", "Rate limit exceeded; wait `Retry-After` seconds", [Catalog.RATE_LIMITED]),
    500: ("InternalError", "An unexpected error; quote `request_id` when reporting it", [Catalog.INTERNAL]),
    503: ("ServiceUnavailable", "The database cannot be reached (retry after `Retry-After` seconds), "
                                "or ingest is not configured on this deployment",
          [Catalog.DATABASE_UNAVAILABLE, Catalog.INGEST_DISABLED]),
}
_RETRY = {"Retry-After": {"description": "Seconds to wait before retrying", "schema": {"type": "integer"}}}


def responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """For a route's `responses=`: references into components.responses."""
    return {s: {"$ref": f"#/components/responses/{RESPONSES[s][0]}"} for s in statuses}


def components() -> dict[str, Any]:
    out = {}
    for status, (name, description, problems) in RESPONSES.items():
        p = problems[0]
        example = {"type": p.type, "title": p.title, "status": p.status, "detail": _EXAMPLE_DETAIL[p],
                   "instance": "/v1/registers/gb_ukgc/tables/licences/rows",
                   "request_id": "0199c1a8-7c3e-7a52-9d1e-5b6f0c2a4e11"}
        if p is Catalog.INVALID_PARAMETER:
            example["errors"] = [{"field": "limit", "location": "query",
                                  "message": "Input should be less than or equal to 1000"}]
        out[name] = {
            "description": description + ". Types: " + ", ".join(f"`{q.slug}`" for q in problems),
            "content": {MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}, "example": example}},
            **({"headers": _RETRY} if status in (409, 429, 503) else {}),
        }
    return out


_EXAMPLE_DETAIL = {
    Catalog.INVALID_PARAMETER: "limit: Input should be less than or equal to 1000",
    Catalog.UNAUTHENTICATED: "bad or missing bearer token",
    Catalog.FORBIDDEN: "a read token cannot start or list ingest runs",
    Catalog.INGEST_IN_PROGRESS: "an ingest run is already going: /v1/ingest-runs/0199c1a8-7c3e-7a52-9d1e-5b6f0c2a4e11",
    Catalog.UNSUPPORTED_MEDIA_TYPE: "send the body as application/json, not text/plain",
    Catalog.IDEMPOTENCY_KEY_REUSED: "Idempotency-Key 'cron-2026-10-07' was used with a different body",
    Catalog.JURISDICTION_NOT_FOUND: "unknown jurisdiction 'zz'; known: au, be, ca, ...",
    Catalog.RATE_LIMITED: "60 requests per minute for /v1/search; retry in 12 s",
    Catalog.INTERNAL: "something went wrong on our side; quote request_id if you report it",
    Catalog.DATABASE_UNAVAILABLE: "the database cannot be reached right now; retry shortly",
}
