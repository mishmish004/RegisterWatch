"""Opaque page cursors.

A cursor is base64url JSON, and clients must treat it as an opaque token: what
is inside may change between releases without changing the contract. Each one
is bound to the query that produced it (`scope`), so a cursor replayed with
different filters is refused rather than silently paging a different result.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any


class InvalidCursor(ValueError):
    pass


def scope_of(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def encode(position: dict[str, Any], scope: str) -> str:
    raw = json.dumps({"p": position, "s": scope}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode(token: str, scope: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body = json.loads(raw)
        position, got = body["p"], body["s"]
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidCursor("cursor is not one this API issued") from exc
    if got != scope or not isinstance(position, dict):
        raise InvalidCursor("cursor belongs to a different query; start again without it")
    return position
