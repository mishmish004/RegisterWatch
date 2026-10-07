"""Opaque page cursors.

A cursor is base64url JSON, and clients must treat it as an opaque token: what
is inside may change between releases without changing the contract. Each one
carries a tag over the query that produced it (`scope`) and its own position,
so a cursor replayed with different filters, or altered in any byte, is refused
rather than silently paging a different result. The tag is a checksum, not a
signature: a position is only a row id and a snapshot id, nothing to forge.
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


def _tag(position: Any, scope: str) -> str:
    return scope_of(scope, position)


def encode(position: dict[str, Any], scope: str) -> str:
    raw = json.dumps({"p": position, "s": _tag(position, scope)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode(token: str, scope: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body = json.loads(raw)
        position, got = body["p"], body["s"]
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidCursor("cursor is not one this API issued") from exc
    if not isinstance(position, dict) or got != _tag(position, scope):
        raise InvalidCursor("cursor belongs to a different query, or was altered; start again without it")
    return position
