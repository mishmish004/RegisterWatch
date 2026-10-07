"""Conditional reads: ETag, If-None-Match, Cache-Control (plan.md P7.1).

Register data changes only when a snapshot lands, a few times a day, so a read
is named by the snapshots it was read at. A register's data version is its
newest complete snapshot id (current rows, the change feed, search, domains),
or its newest snapshot of any kind for what also shows failed runs (snapshot
history, freshness). Every register's version comes from one query, kept in
the process for 30 s, so answering `If-None-Match` with a 304 touches no
database. A snapshot shows in ETags within those 30 s.

ETag = W/"sha256 of (salt, sorted (slug, version) pairs, path, query)". The
salt is the package version and source, so a deploy that changes how a
response is rendered never revalidates the old rendering. Freshness also counts
failures over a rolling 7 days, which ages without a new snapshot: those ETags
also turn over every max-age (5 minutes), so a revalidated copy is never staler
than a cached one may be.

Ingest and status responses are never stored (`NoStoreMiddleware`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
import threading
import time
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Request, Response

import registerwatch
from registerwatch import __version__
from registerwatch.http import deps
from registerwatch.registers.base import Register

log = logging.getLogger(__name__)

TTL_S = 30.0
RETRY_S = 5.0  # after a failed lookup, reads go untagged this long rather than each waiting on the database
MAX_AGE = 300
VARY = "Authorization, Accept-Encoding"

# Never stored by any cache: starting and watching ingest runs, and the probes
# and status pages (`/v1/status`, `/livez` and `/readyz` arrive in Phase 8).
NO_STORE = re.compile(r"^/(v1/ingest-runs(/.*)?|ingest/.*|jurisdictions/[^/]+/ingest|v1/status|status|health"
                      r"|livez|readyz)$")


class DataVersions:
    """slug -> (newest complete snapshot id, newest snapshot id), refreshed at
    most every `ttl` seconds. None while the database cannot be read, and
    for `RETRY_S` after a failed lookup."""

    def __init__(self, ttl: float = TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._value: dict[str, tuple[int, int]] | None = None
        self._at = self._retry_at = 0.0

    def get(self) -> dict[str, tuple[int, int]] | None:
        with self._lock:
            now = self.clock()
            if self._value is not None and now - self._at < self.ttl:
                return self._value
            if self._value is None and now < self._retry_at:
                return None
            try:
                with deps.connection() as conn:
                    rows = deps.repo.data_versions(conn)
            except Exception:  # noqa: BLE001 — an ETag is an optimisation; the read itself decides the answer
                log.warning("data versions unavailable; answering without an ETag", exc_info=True)
                self._value, self._retry_at = None, now + RETRY_S
                return None
            self._value = {r["slug"]: (int(r["complete"]), int(r["latest"])) for r in rows}
            self._at = now
            return self._value

    def clear(self) -> None:
        with self._lock:
            self._value, self._retry_at = None, 0.0


VERSIONS = DataVersions()
wall: Callable[[], float] = time.time  # for the freshness window; tests move it


@lru_cache
def salt() -> str:
    """The package version and every source file of it: what else, besides the
    data, decides what a response looks like."""
    root = pathlib.Path(registerwatch.__file__).parent
    h = hashlib.sha256(__version__.encode())
    for path in sorted(root.rglob("*.py")):
        h.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return h.hexdigest()


class NotModified(Exception):
    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__("not modified")
        self.headers = headers


def cache_control() -> str:
    # Behind a read token a response is for its caller only; open data is for anyone.
    return f"{'private' if deps.settings().read_token else 'public'}, max-age={MAX_AGE}"


def conditional(request: Request, response: Response, registers: Iterable[Register] = (), *,
                attempts: bool = False, freshness: bool = False) -> None:
    """Mark this read cacheable and tag it with the data it depends on: no
    registers for what only the code decides (the register catalogue), the
    newest complete snapshot of each register otherwise; `attempts` counts every
    snapshot (history), `freshness` also the rolling window. Raises NotModified
    (a 304) when the client's `If-None-Match` already names this version. Call it
    once the request is known to be valid, before reading any rows."""
    response.headers["Cache-Control"] = cache_control()
    response.headers["Vary"] = VARY
    parts: list[Any] = []
    slugs = sorted({r.slug for r in registers})
    if slugs:
        versions = VERSIONS.get()
        if versions is None:
            return
        which = 1 if attempts or freshness else 0
        parts = [[slug, versions.get(slug, (0, 0))[which]] for slug in slugs]
    if freshness:
        parts.append(int(wall() // MAX_AGE))
    raw = json.dumps([salt(), parts, request.url.path, request.url.query], separators=(",", ":"))
    tag = f'W/"{hashlib.sha256(raw.encode()).hexdigest()}"'
    response.headers["ETag"] = tag
    if matches(request.headers.get("if-none-match"), tag):
        raise NotModified({k: response.headers[k] for k in ("ETag", "Cache-Control", "Vary")})


def matches(if_none_match: str | None, tag: str) -> bool:
    """RFC 9110 13.1.2: `*`, or any listed tag equal to ours by weak comparison."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    ours = tag.removeprefix("W/")
    return any(t.strip().removeprefix("W/") == ours for t in if_none_match.split(","))


async def _on_not_modified(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, NotModified)
    return Response(status_code=304, headers=exc.headers)


def install(app: FastAPI) -> None:
    app.add_exception_handler(NotModified, _on_not_modified)


class NoStoreMiddleware:
    """`Cache-Control: no-store` on every answer from an ingest or status path,
    errors included. A plain ASGI middleware, so background tasks are untouched."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not NO_STORE.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        async def no_store(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, receive, no_store)


# --- the spec ------------------------------------------------------------------------

HEADERS = {
    "ETag": {"description": "This representation's version (weak). Send it back in `If-None-Match` and the "
                            "answer is a 304 with no body while the data is unchanged. New data shows within "
                            "30 seconds of its snapshot.",
             "schema": {"type": "string"}, "example": 'W/"9f2c5d0e..."'},
    "Cache-Control": {"description": "`public, max-age=300` when reads are open, `private, max-age=300` when the "
                                     "deployment requires a read token; `no-store` on ingest operations",
                      "schema": {"type": "string"}},
}
NOT_MODIFIED = {
    "description": "Unchanged since the `ETag` sent in `If-None-Match`; no body",
    "headers": {"ETag": {"$ref": "#/components/headers/ETag"},
                "Cache-Control": {"$ref": "#/components/headers/Cache-Control"}},
}
