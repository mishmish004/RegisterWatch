"""The only place that makes outbound requests.

Every attempt produces one log entry. That entry is simultaneously an ops metric,
an input to the completeness gate, and a legal artifact — so it is written here,
by the thing that did the waiting, not by the runner.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from registerwatch.config import settings
from registerwatch.fetch import limiter, robots
from registerwatch.fetch.limiter import RateBudget

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class Request:
    url: str
    ordinal: int
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    label: str = ""  # e.g. "page-3", "bulk-csv"


@dataclass
class Fetched:
    request: Request
    status_code: int
    body: bytes | None
    content_hash: bytes | None
    headers: dict[str, str]
    log: list[dict[str, Any]]           # one entry per attempt
    not_modified: bool = False

    @property
    def ok(self) -> bool:
        return self.status_code in (200, 304)


def _entry(**kw: Any) -> dict[str, Any]:
    return {"logged_at": datetime.now(timezone.utc).isoformat(), **kw}


def fetch_one(
    client: httpx.Client,
    req: Request,
    budget: RateBudget,
    validators: dict[str, str] | None = None,
) -> Fetched:
    host = urlsplit(req.url).netloc
    log: list[dict[str, Any]] = []

    decision = robots.check(req.url)
    if not decision.allowed:
        log.append(_entry(url=req.url, ordinal=req.ordinal, attempt_no=0,
                          robots_decision="disallowed", status_code=None,
                          error_class="RobotsDisallowed"))
        return Fetched(req, 0, None, None, {}, log)

    headers = {"User-Agent": settings().user_agent, **req.headers}
    conditional = False
    if validators:
        if etag := validators.get("etag"):
            headers["If-None-Match"] = etag
            conditional = True
        if lm := validators.get("last_modified"):
            headers["If-Modified-Since"] = lm
            conditional = True

    last: Fetched | None = None
    for attempt in range(1, settings().max_attempts + 1):
        res = limiter.reserve(host, budget, decision.crawl_delay_ms)
        started = time.monotonic()
        status: int | None = None
        err: str | None = None
        body: bytes | None = None
        resp_headers: dict[str, str] = {}
        retry_after: int | None = None

        try:
            resp = client.request(req.method, req.url, headers=headers)
            status = resp.status_code
            resp_headers = dict(resp.headers)
            if status == 200:
                body = resp.content
            if status == 429:
                try:
                    retry_after = int(resp.headers.get("Retry-After", "0")) or None
                except ValueError:
                    retry_after = None
        except httpx.HTTPError as exc:
            err = type(exc).__name__

        duration_ms = int((time.monotonic() - started) * 1000)
        log.append(_entry(
            url=req.url,
            label=req.label,
            ordinal=req.ordinal,
            attempt_no=attempt,
            requested_at=res.slot_at.isoformat(),
            bucket_wait_ms=res.waited_ms,
            duration_ms=duration_ms,
            status_code=status,
            bytes=len(body) if body else 0,
            etag=resp_headers.get("etag"),
            last_modified=resp_headers.get("last-modified"),
            retry_after_s=retry_after,
            conditional=conditional,
            not_modified=status == 304,
            robots_decision=decision.reason,
            user_agent=settings().user_agent,
            error_class=err,
        ))

        if status == 429:
            # Not a normal retry path. If the budget were right we'd never be here.
            limiter.penalise(host, budget, retry_after)

        digest = hashlib.sha256(body).digest() if body is not None else None
        last = Fetched(req, status or 0, body, digest, resp_headers, log,
                       not_modified=status == 304)

        if status is not None and status not in RETRYABLE:
            return last
        if attempt < settings().max_attempts:
            time.sleep(min(2 ** attempt, 30))

    assert last is not None
    return last
