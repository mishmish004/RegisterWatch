"""What every answer carries on the wire, whatever route made it (plan.md P10.1,
P10.4, P10.5). Plain ASGI middlewares, like the others, so background tasks are
untouched.

  SecurityHeadersMiddleware  `X-Content-Type-Options: nosniff`, `Referrer-Policy:
                             no-referrer` and a Content-Security-Policy on every
                             answer; `Strict-Transport-Security` when the request
                             came over https (the platform's proxy says so in
                             `X-Forwarded-Proto`, believed from FORWARDED_ALLOW_IPS)
  CompressionMiddleware      gzip for bodies of 1 KiB and more, when the client
                             accepts it
  CorsMiddleware             none by default; CORS_ALLOW_ORIGINS lets those
                             origins read with GET, without credentials

The API serves JSON, so its policy forbids everything (`default-src 'none'`):
a response opened in a browser runs nothing and cannot be framed. The only HTML
is the documentation (`/docs`, `/redoc`, `/docs/oauth2-redirect`), which loads
Swagger UI or Redoc from jsDelivr and fetches /openapi.json: its policy allows
exactly that, and its inline scripts by their hashes, taken from the page as
served.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from registerwatch.config import settings

HSTS = "max-age=31536000"
CSP = "default-src 'none'; frame-ancestors 'none'"
# The documentation pages: Swagger UI and Redoc from jsDelivr, Redoc's fonts from
# Google and its web worker from a blob, FastAPI's favicon, the spec from here.
# Redoc writes its styles at run time, so styles can only be allowed inline
# wholesale; scripts are allowed by hash. (Browsers ignore 'unsafe-inline' where
# a hash is listed, so the two lists are kept apart.)
DOCS_CSP = ("default-src 'none'; script-src https://cdn.jsdelivr.net{hashes}; "
            "style-src 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
            "worker-src blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
_FIXED = ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"))
_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)

# gzip (P10.4): below 1 KiB a body gains little and the header costs about as
# much. Level 6, zlib's default: on a 1000-row page (325 KB) it takes 2.5 ms for
# 8.9 %, where level 9 (Starlette's default) takes 10 ms for 8.6 %.
GZIP_MIN_BYTES = 1024
GZIP_LEVEL = 6

# CORS (P10.5): reading only. A browser may send these with a GET, and read these
# from the answer (beyond the ones every browser lets through).
CORS_ALLOW_HEADERS = ("Authorization", "If-None-Match", "X-Request-Id")
CORS_EXPOSE_HEADERS = ("ETag", "Link", "RateLimit", "RateLimit-Policy", "Retry-After", "X-Request-Id")
CORS_MAX_AGE_S = 600


def docs_csp(html: bytes) -> str:
    hashes = "".join(f" 'sha256-{base64.b64encode(hashlib.sha256(s).digest()).decode()}'"
                     for s in _INLINE_SCRIPT.findall(html))
    return DOCS_CSP.format(hashes=hashes)


def _secured(headers: list[tuple[bytes, bytes]], https: bool, csp: str) -> list[tuple[bytes, bytes]]:
    have = {k.lower() for k, _ in headers}
    extra = [(k, v) for k, v in _FIXED if k not in have]
    if b"content-security-policy" not in have:
        extra.append((b"content-security-policy", csp.encode()))
    if https and b"strict-transport-security" not in have:
        extra.append((b"strict-transport-security", HSTS.encode()))
    return [*headers, *extra]


class SecurityHeadersMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https"
        start: dict | None = None
        html: list[bytes] = []

        async def secured(message: dict) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                kind = next((v for k, v in message.get("headers", []) if k.lower() == b"content-type"), b"")
                if kind.startswith(b"text/html"):
                    start = message  # held until the page is whole: its policy names its scripts
                    return
                message = {**message, "headers": _secured(list(message.get("headers", [])), https, CSP)}
            elif start is not None and message["type"] == "http.response.body":
                html.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                page = b"".join(html)
                await send({**start, "headers": _secured(list(start.get("headers", [])), https, docs_csp(page))})
                message = {**message, "body": page}
            await send(message)

        await self.app(scope, receive, secured)


class CompressionMiddleware(GZipMiddleware):
    """Starlette's gzip, with `Vary` listing each field once: it appends
    `Accept-Encoding` to a `Vary` that may already name it (http/caching.py)."""

    def __init__(self, app: Any) -> None:
        super().__init__(app, minimum_size=GZIP_MIN_BYTES, compresslevel=GZIP_LEVEL)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return

        async def one_vary(message: dict) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": _merge_vary(message.get("headers", []))}
            await send(message)

        await super().__call__(scope, receive, one_vary)


def _merge_vary(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    fields: dict[str, str] = {}
    for k, v in headers:
        if k.lower() == b"vary":
            for f in v.decode("latin-1").split(","):
                if f.strip():
                    fields.setdefault(f.strip().lower(), f.strip())
    if not fields:
        return list(headers)
    return [(k, v) for k, v in headers if k.lower() != b"vary"] + [(b"vary", ", ".join(fields.values()).encode())]


class CorsMiddleware:
    """Off unless CORS_ALLOW_ORIGINS names origins (read per request, like every
    setting). Then Starlette's CORS for GET and HEAD only, without credentials:
    a preflight for any other method is refused, and a request with any other
    method gets no `Access-Control-Allow-Origin`, so a browser lets no page read
    its answer."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._cors: dict[tuple[str, ...], CORSMiddleware] = {}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        origins = settings().cors_origins if scope["type"] == "http" else ()
        if not origins or scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        cors = self._cors.get(origins)
        if cors is None:
            cors = self._cors[origins] = CORSMiddleware(
                self.app, allow_origins=list(origins), allow_methods=["GET", "HEAD"],
                allow_headers=list(CORS_ALLOW_HEADERS), expose_headers=list(CORS_EXPOSE_HEADERS),
                allow_credentials=False, max_age=CORS_MAX_AGE_S)
        await cors(scope, receive, send)
