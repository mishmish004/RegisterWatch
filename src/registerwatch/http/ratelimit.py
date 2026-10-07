"""Rate limits (plan.md P7.2): a token bucket per client and class of request.

The client is the bearer token when it is one this deployment issued (by its
hash: the token itself is never kept), else the client address. Uvicorn sets
that address from `X-Forwarded-For` only for proxies in `FORWARDED_ALLOW_IPS`
(P9.2, P10.2); until then, behind a platform proxy every anonymous client is the
proxy. An unknown token counts as its address, so inventing tokens buys nothing.

  read    every other request             RATE_LIMIT_READ_PER_MIN    (600)
  search  /v1/search, /v1/domains         RATE_LIMIT_SEARCH_PER_MIN  (60)
  ingest  starting an ingest run          RATE_LIMIT_INGEST_PER_MIN  (10)

Probes (/livez, /readyz, legacy /health) are never limited. A bucket holds a
minute's quota and refills continuously, so a client can burst up to the
quota, then gets one request per (60 / quota) seconds. Buckets live in the
process: each replica, and each worker, counts on its own. That is acceptable
at this scale and needs no shared store.

Every limited response says where its client stands, in the IETF httpapi
draft's fields (draft-ietf-httpapi-ratelimit-headers-11):

  RateLimit-Policy: "search";q=60;w=60
  RateLimit: "search";r=41;t=1        r requests left, more in t seconds

Over the quota: 429 `rate-limited` (FastAPI's `{"detail": ...}` on legacy
routes) with `Retry-After`. A plain ASGI middleware, so background tasks are
untouched.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from registerwatch.config import settings
from registerwatch.http import problems

WINDOW_S = 60
MAX_BUCKETS = 20_000  # least recently used goes first; an evicted bucket was refilling anyway
EXEMPT = frozenset({"/livez", "/readyz", "/health"})
_SEARCH = re.compile(r"^/(v1/search|v1/domains/.*|search|check/domain/.*)$")
_START_INGEST = re.compile(r"^/(v1/ingest-runs|ingest/[^/]+|jurisdictions/[^/]+/ingest)$")
_WHAT = {"read": "reads", "search": "searches and domain checks", "ingest": "starting ingest runs"}


def policy_of(method: str, path: str) -> str | None:
    """Which bucket a request draws on; None for an unlimited one."""
    if path in EXEMPT:
        return None
    if method == "POST" and _START_INGEST.match(path):
        return "ingest"
    if _SEARCH.match(path):
        return "search"
    return "read"


def quota(policy: str) -> int:
    s = settings()
    return {"read": s.rate_limit_read_per_min, "search": s.rate_limit_search_per_min,
            "ingest": s.rate_limit_ingest_per_min}[policy]


def client_of(scope: dict) -> str:
    """`token:<hash>` for a bearer token this deployment issued, else `ip:<address>`.
    The presented token is hashed, then looked up: no comparison ever runs on
    the secret itself, so there is nothing to time."""
    authorization = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"authorization"), "")
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() == "bearer" and credentials:
        ours = {_digest(t) for t in (settings().read_token, settings().ingest_token) if t}
        presented = _digest(credentials)
        if presented in ours:
            return f"token:{presented[:16]}"
    client = scope.get("client")
    return f"ip:{client[0] if client else 'unknown'}"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Buckets:
    """(policy, client) -> [tokens, when last counted]."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, size: int = MAX_BUCKETS) -> None:
        self.clock, self.size = clock, size
        self._lock = threading.Lock()
        self._buckets: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

    def take(self, policy: str, client: str, limit: int) -> tuple[bool, float]:
        """Spend one request if there is one: (whether there was, what is left)."""
        rate = limit / WINDOW_S
        with self._lock:
            now = self.clock()
            tokens, then = self._buckets.pop((policy, client), (float(limit), now))
            tokens = min(float(limit), tokens + (now - then) * rate)
            allowed = tokens >= 1
            if allowed:
                tokens -= 1
            self._buckets[(policy, client)] = [tokens, now]
            if len(self._buckets) > self.size:
                self._buckets.popitem(last=False)
        return allowed, tokens

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


BUCKETS = Buckets()


def headers(policy: str, limit: int, tokens: float) -> dict[str, str]:
    """The draft's fields; `t` is when the next request's worth is back."""
    rate = limit / WINDOW_S
    left = math.floor(tokens)
    wait = 0 if tokens >= limit else math.ceil((left + 1 - tokens) / rate)
    return {"RateLimit-Policy": f'"{policy}";q={limit};w={WINDOW_S}',
            "RateLimit": f'"{policy}";r={left};t={wait}'}


class RateLimitMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        policy = policy_of(scope.get("method", ""), scope.get("path", "")) if scope["type"] == "http" else None
        limit = quota(policy) if policy else 0
        if not limit:
            await self.app(scope, receive, send)
            return
        allowed, tokens = BUCKETS.take(policy, client_of(scope), limit)
        fields = headers(policy, limit, tokens)
        if not allowed:
            fields["Retry-After"] = fields["RateLimit"].rsplit("t=", 1)[1]
            await self._refuse(scope, policy, limit, fields)(scope, receive, send)
            return
        extra = [(k.lower().encode(), v.encode()) for k, v in fields.items()]

        async def counted(message: dict) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, counted)

    @staticmethod
    def _refuse(scope: dict, policy: str, limit: int, fields: dict[str, str]):
        request = Request(scope)
        detail = f"{limit} requests per minute for {_WHAT[policy]}; retry in {fields['Retry-After']} s"
        if problems.is_v1(request):
            return problems.response(problems.Catalog.RATE_LIMITED, detail, request, headers=fields)
        return JSONResponse({"detail": detail}, status_code=429, headers=fields)


# --- the spec ------------------------------------------------------------------------

HEADERS = {
    "RateLimit-Policy": {
        "description": "The quota this request counts against (draft-ietf-httpapi-ratelimit-headers): "
                       '`"read";q=600;w=60` is 600 requests per 60 seconds. Per bearer token, or per client '
                       "address without one. Defaults: `read` 600, `search` (search and domains) 60, `ingest` "
                       "(starting runs) 10 a minute.",
        "schema": {"type": "string"}, "example": '"search";q=60;w=60'},
    "RateLimit": {
        "description": "Where the client stands: `r` requests left, more available in `t` seconds",
        "schema": {"type": "string"}, "example": '"search";r=41;t=1'},
}
