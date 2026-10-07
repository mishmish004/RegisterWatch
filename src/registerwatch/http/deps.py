"""What every v1 route shares: a database transaction, the read check,
parameter guards, and turning identifiers into registers and tables (or a 404).

The legacy routes in `registerwatch.api` keep their own copies of the token
checks until they are removed, so tests that patch that module keep meaning
what they meant. Both read the same settings.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import AbstractContextManager
from typing import Any

from fastapi import Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import Connection
from pydantic import AwareDatetime

from registerwatch import jurisdictions, registers
from registerwatch.config import settings
from registerwatch.db.engine import tx
from registerwatch.db.repos import snapshots as repo
from registerwatch.http.problems import Catalog, ProblemError, invalid
from registerwatch.registers.base import Register, Table

log = logging.getLogger(__name__)

JURISDICTION: Any = Query(None, description="Only these jurisdictions; repeat the parameter for several "
                                            "(`?jurisdiction=gb&jurisdiction=de`)")

# The type of every timestamp parameter: RFC 3339 with an offset
# (2026-10-01T00:00:00Z). A naive value is a 400, never silently read as UTC.
Timestamp = AwareDatetime


def connection() -> AbstractContextManager[Connection]:
    """One transaction. `tx` is looked up per call, so tests can patch it here."""
    return tx()


# The two bearer schemes the spec declares. Both read the one Authorization
# header; `auto_error=False` leaves every refusal to the checks below, so it is
# a problem with the right status and `WWW-Authenticate`, not FastAPI's 403.
READ_TOKEN = HTTPBearer(
    scheme_name="ReadToken", auto_error=False,
    description="`READ_TOKEN`. Needed on read routes only when the server sets one; when it does not, "
                "reads are open. The ingest token reads too.")
INGEST_TOKEN = HTTPBearer(
    scheme_name="IngestToken", auto_error=False,
    description="`INGEST_TOKEN`. Starts and lists ingest runs, and reads. A read token here is a 403.")

_CHALLENGE = 'Bearer realm="registerwatch"'


def token_matches(given: str, *candidates: str) -> list[bool]:
    """Compare against every configured token, each in constant time, without
    stopping at the first match. Bytes, because `compare_digest` raises on a
    non-ASCII str and a header can carry Latin-1."""
    return [secrets.compare_digest(given.encode(), c.encode()) for c in candidates if c]


def unauthenticated(presented: bool) -> ProblemError:
    """401. A token that was sent but is not known gets `error="invalid_token"`
    (RFC 6750 3.1); a request with no bearer token gets the bare challenge."""
    challenge = _CHALLENGE + (', error="invalid_token"' if presented else "")
    detail = "the bearer token is not valid here" if presented else "send Authorization: Bearer <token>"
    return ProblemError(Catalog.UNAUTHENTICATED, detail, headers={"WWW-Authenticate": challenge})


def require_read(creds: HTTPAuthorizationCredentials | None = Security(READ_TOKEN),
                 _: HTTPAuthorizationCredentials | None = Security(INGEST_TOKEN)) -> None:
    """Open unless READ_TOKEN is set; then the read or the ingest token. The second
    parameter is the same header, declared so the spec offers either scheme."""
    token = settings().read_token
    if not token:
        return
    if creds is None:
        raise unauthenticated(False)
    if not any(token_matches(creds.credentials, token, settings().ingest_token)):
        raise unauthenticated(True)


def require_ingest(creds: HTTPAuthorizationCredentials | None = Security(INGEST_TOKEN)) -> None:
    """The ingest token. A valid read token is a 403, not a 401: it is a token,
    just not one that can do this."""
    token = settings().ingest_token
    if not token:
        raise ProblemError(Catalog.INGEST_DISABLED, "INGEST_TOKEN is not configured; ingest is switched off")
    if creds is None:
        raise unauthenticated(False)
    ingest, *read = token_matches(creds.credentials, token, settings().read_token)
    if ingest:
        return
    if any(read):
        raise ProblemError(Catalog.FORBIDDEN, "a read token cannot start or list ingest runs")
    raise unauthenticated(True)


def json_body_only(request: Request) -> None:
    """A body, when there is one, must be JSON (415 otherwise, not a parse error)."""
    has_body = request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers
    kind = request.headers.get("content-type", "application/json").split(";")[0].strip().lower()
    if has_body and not (kind == "application/json" or (kind.startswith("application/") and kind.endswith("+json"))):
        raise ProblemError(Catalog.UNSUPPORTED_MEDIA_TYPE, f"send the body as application/json, not {kind}")


def known_parameters_only(request: Request) -> None:
    """Refuse query parameters the operation does not declare, so a typo
    (`?jurisdictions=gb`) is a 400 instead of a silently unfiltered answer.
    The spec is the list of what is declared; every v1 route names its operation."""
    op_id = getattr(request.scope.get("route"), "operation_id", None)
    ops = (op for item in request.app.openapi()["paths"].values() for op in item.values())
    op = next((o for o in ops if o.get("operationId") == op_id), None)
    if op is None:  # a route without an explicit id; test_operation_ids_are_explicit forbids it
        raise LookupError(f"no operation {op_id!r} in the spec")
    declared = {p["name"]: p for p in op.get("parameters", []) if p.get("in") == "query"}
    if any(p.get("style") == "deepObject" for p in declared.values()):
        return  # free-form filters: the route checks its own query string, with hints
    for key in request.query_params:
        if key not in declared:
            raise invalid(key, f"not a parameter of this operation; it takes: {', '.join(declared) or 'none'}")


def jurisdiction_or_404(code: str) -> list[Register]:
    try:
        return jurisdictions.resolve(code)
    except KeyError as exc:
        raise ProblemError(Catalog.JURISDICTION_NOT_FOUND, exc.args[0]) from None


def registers_for(codes: list[str] | None) -> list[Register]:
    """Every register, or those of the given jurisdictions, each once. An unknown
    code here is a bad parameter (400), not a missing resource."""
    if not codes:
        return registers.all_registers()
    seen: dict[str, Register] = {}
    for code in codes:
        try:
            regs = jurisdictions.resolve(code)
        except KeyError as exc:
            raise invalid("jurisdiction", exc.args[0]) from None
        for r in regs:
            seen.setdefault(r.slug, r)
    return list(seen.values())


def register_or_404(slug: str) -> Register:
    try:
        return registers.get(slug)
    except KeyError as exc:
        raise ProblemError(Catalog.REGISTER_NOT_FOUND, exc.args[0]) from None


def table_or_404(register: Register, name: str) -> Table:
    for t in register.tables:
        if t.name == name:
            return t
    raise ProblemError(Catalog.TABLE_NOT_FOUND, f"unknown table {name!r} in {register.slug}; "
                                                f"tables: {', '.join(t.name for t in register.tables)}")


def table_of(slug: str, name: str) -> Table:
    """A table known to exist (it came back from a query over declared tables)."""
    return next(t for t in registers.get(slug).tables if t.name == name)


def freshness() -> dict[str, dict[str, Any]] | None:
    """Per-register health rows, or None when the database cannot be reached.
    Descriptions of registers are still useful without it."""
    try:
        with connection() as conn:
            return {r["slug"]: r for r in repo.source_health(conn)}
    except Exception:  # noqa: BLE001 — degrade to "freshness unknown", never fail the description
        log.warning("freshness unavailable", exc_info=True)
        return None
