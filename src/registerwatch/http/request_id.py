"""One id per request: taken from `X-Request-Id` when it is safe to reuse,
generated (UUIDv7, so ids sort by time) when not, echoed on every response,
carried into problem bodies and log records.

A plain ASGI middleware rather than BaseHTTPMiddleware, so background tasks
(the ingest runs after its 202) are untouched.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import time
import uuid
from typing import Any

HEADER = "x-request-id"
# Short and boring: it ends up in logs, so nothing that could forge a log line.
_SAFE = re.compile(r"^[A-Za-z0-9._:/+=-]{1,128}$")
_current: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def uuid7() -> str:
    """RFC 9562 version 7: 48 bits of Unix milliseconds, then random bits."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | (rand >> 68 & 0xFFF) << 64 | 0b10 << 62 | rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def current() -> str:
    return _current.get()


def choose(supplied: str | None) -> str:
    return supplied if supplied and _SAFE.match(supplied) else uuid7()


class RequestIdMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == HEADER.encode()), None)
        rid = choose(supplied)
        token = _current.set(rid)

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != HEADER.encode()]
                message = {**message, "headers": [*headers, (HEADER.encode(), rid.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _current.reset(token)


def install_log_field() -> None:
    """Every log record gets `request_id` ("-" outside a request), so formats can use it."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_registerwatch_request_id", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.request_id = _current.get()
        return record

    factory._registerwatch_request_id = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
