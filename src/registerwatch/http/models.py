"""Every v1 response shape. These are the OpenAPI schemas clients are built from.

Conventions (plan.md §2.3): snake_case fields, `_at` for timestamps (RFC 3339,
UTC), `url` for the resource's own path, and every collection as
`{"data": [...], "pagination": {...}}`. Examples are real rows from the
register fixtures.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from registerwatch import jurisdictions
from registerwatch.registers import base

# What a register column can hold, once it is JSON: text, numbers, booleans,
# dates and timestamps as RFC 3339 strings, text arrays, or null.
Value = str | int | float | bool | datetime | date | list[str] | None


def _examples(*examples: dict[str, Any]) -> ConfigDict:
    return ConfigDict(json_schema_extra={"examples": list(examples)}, validate_by_name=True)


# `register` is the natural field name, but pydantic's BaseModel already has a
# `register` attribute; the field is `register_` in Python and `register` on the wire.
REGISTER: Any = Field(alias="register", description="Register slug, also its Postgres schema")


# --- errors (RFC 9457; the catalog is http/problems.py) -----------------------------

class FieldError(BaseModel):
    model_config = _examples({"field": "limit", "location": "query",
                              "message": "Input should be less than or equal to 1000"})
    field: str
    location: str = Field(description="`query`, `path` or `header`")
    message: str


class Problem(BaseModel):
    """RFC 9457 Problem Details. Extension members may be added per type."""

    model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [{
        "type": "https://github.com/mishmish004/RegisterWatch/blob/main/docs/problems.md#invalid-parameter",
        "title": "Invalid parameter", "status": 400, "detail": "limit: Input should be less than or equal to 1000",
        "instance": "/v1/registers/gb_ukgc/tables/licences/rows",
        "request_id": "0199c1a8-7c3e-7a52-9d1e-5b6f0c2a4e11",
        "errors": [{"field": "limit", "location": "query", "message": "Input should be less than or equal to 1000"}],
    }]})
    type: str = Field(json_schema_extra={"format": "uri"},
                      description="Stable identifier of the problem type; its docs say what to do")
    title: str
    status: int
    detail: str = Field(description="What went wrong with this request, for a human")
    instance: str = Field(json_schema_extra={"format": "uri-reference"}, description="The request path")
    request_id: str = Field(description="Also in the `X-Request-Id` response header and the server's logs")
    errors: list[FieldError] | None = Field(default=None, description="Each invalid parameter, for 400s")


# --- pagination ----------------------------------------------------------------

class Pagination(BaseModel):
    model_config = _examples({"next_cursor": "eyJwIjp7Im8iOjEwMH0sInMiOiI0ZjFkIn0", "has_more": True,
                              "limit": 100, "total": None})
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null on the last page")
    has_more: bool
    limit: int = Field(description="Page size this response was read with")
    total: int | None = Field(default=None, description="Matching items; only with `include_total=true` "
                                                        "on endpoints that offer it, else null")

    @classmethod
    def whole(cls, items: list[Any]) -> Pagination:
        """A collection small enough to return in one page."""
        return cls(next_cursor=None, has_more=False, limit=len(items), total=len(items))


# --- registers and tables --------------------------------------------------------

class Column(BaseModel):
    model_config = _examples({"name": "licence_number", "type": "text", "required": True})
    name: str
    type: base.ColumnType
    required: bool = Field(description="Filled in on (nearly) every row of a good snapshot")


class TableSchema(BaseModel):
    model_config = _examples({
        "register": "gb_ukgc", "name": "licences",
        "description": "One row per licence and activity; licence_number's last segment is a version",
        "columns": [{"name": "account_number", "type": "text", "required": True},
                    {"name": "licence_number", "type": "text", "required": True},
                    {"name": "status", "type": "text", "required": True},
                    {"name": "start_date", "type": "date", "required": False}],
        "url": "/v1/registers/gb_ukgc/tables/licences",
        "rows_url": "/v1/registers/gb_ukgc/tables/licences/rows",
    })
    register_: str = REGISTER
    name: str
    description: str | None
    columns: list[Column]
    url: str
    rows_url: str

    @classmethod
    def of(cls, register: base.Register, table: base.Table) -> TableSchema:
        url = f"/v1/registers/{register.slug}/tables/{table.name}"
        return cls(register_=register.slug, name=table.name, description=table.description or None,
                   columns=[Column(name=c.name, type=c.type, required=c.required) for c in table.columns],
                   url=url, rows_url=f"{url}/rows")


class RegisterSummary(BaseModel):
    model_config = _examples({"slug": "gb_ukgc", "jurisdiction": "GB", "regulator": "Gambling Commission",
                              "name": "Public register of gambling businesses", "kind": "licensees",
                              "url": "/v1/registers/gb_ukgc"})
    slug: str
    jurisdiction: str
    regulator: str
    name: str
    kind: base.RegisterKind
    url: str

    @classmethod
    def of(cls, r: base.Register) -> RegisterSummary:
        return cls(slug=r.slug, jurisdiction=jurisdictions.normalise(r.country), regulator=r.regulator,
                   name=r.name, kind=r.kind, url=f"/v1/registers/{r.slug}")


_REGISTER_EXAMPLE = {
    "slug": "ch_esbk", "jurisdiction": "CH", "regulator": "Eidgenössische Spielbankenkommission (ESBK)",
    "name": "Blocklist of unauthorised online casino games", "kind": "blocklist",
    "homepage": "https://www.esbk.admin.ch/en/unauthorised-online-games", "url": "/v1/registers/ch_esbk",
    "tables": [{"register": "ch_esbk", "name": "blocked_domains", "description": None,
                "columns": [{"name": "domain", "type": "text", "required": True},
                            {"name": "listed_on", "type": "date", "required": False}],
                "url": "/v1/registers/ch_esbk/tables/blocked_domains",
                "rows_url": "/v1/registers/ch_esbk/tables/blocked_domains/rows"}],
}


class Register(BaseModel):
    model_config = _examples(_REGISTER_EXAMPLE)
    slug: str
    jurisdiction: str
    regulator: str
    name: str
    kind: base.RegisterKind
    homepage: str
    url: str
    tables: list[TableSchema]

    @classmethod
    def of(cls, r: base.Register) -> Register:
        return cls(**RegisterSummary.of(r).model_dump(), homepage=r.homepage,
                   tables=[TableSchema.of(r, t) for t in r.tables])


class Freshness(BaseModel):
    model_config = _examples({"last_attempt_at": "2026-10-07T06:00:41Z", "last_good_at": "2026-10-07T06:00:41Z",
                              "last_reason": None, "failed_7d": 0})
    last_attempt_at: datetime | None = Field(description="Newest ingest attempt, good or not")
    last_good_at: datetime | None = Field(description="Newest complete snapshot; null if there has never been one")
    last_reason: str | None = Field(description="Why the newest attempt was incomplete, e.g. "
                                                "`PAGE_GAP:3/4 licences:HTTP_503`; null if it was complete")
    failed_7d: int = Field(description="Incomplete attempts in the last 7 days")

    @classmethod
    def of(cls, health: dict[str, Any] | None) -> Freshness:
        h = health or {}
        return cls(last_attempt_at=h.get("last_fetch"), last_good_at=h.get("last_good"),
                   last_reason=h.get("last_reason"), failed_7d=h.get("failed_7d") or 0)


class RegisterDetail(Register):
    model_config = _examples({**_REGISTER_EXAMPLE, "freshness": {
        "last_attempt_at": "2026-10-07T06:00:41Z", "last_good_at": "2026-10-07T06:00:41Z",
        "last_reason": None, "failed_7d": 0}})
    freshness: Freshness | None = Field(description="Null when the database cannot be reached")

    @classmethod
    def with_health(cls, r: base.Register, health: dict[str, dict[str, Any]] | None) -> RegisterDetail:
        return cls(**Register.of(r).model_dump(),
                   freshness=None if health is None else Freshness.of(health.get(r.slug)))


class RegisterPage(BaseModel):
    model_config = _examples({"data": [_REGISTER_EXAMPLE], "pagination": {
        "next_cursor": None, "has_more": False, "limit": 21, "total": 21}})
    data: list[Register]
    pagination: Pagination


# --- jurisdictions ---------------------------------------------------------------

_JURISDICTION_EXAMPLE = {
    "code": "CH", "name": "Switzerland", "url": "/v1/jurisdictions/ch",
    "registers": [
        {"slug": "ch_esbk", "jurisdiction": "CH", "regulator": "Eidgenössische Spielbankenkommission (ESBK)",
         "name": "Blocklist of unauthorised online casino games", "kind": "blocklist",
         "url": "/v1/registers/ch_esbk"},
        {"slug": "ch_gespa", "jurisdiction": "CH", "regulator": "Gespa — Interkantonale Geldspielaufsicht",
         "name": "Blocklist of unauthorised lottery and betting sites", "kind": "blocklist",
         "url": "/v1/registers/ch_gespa"}],
}


def _jurisdiction_url(code: str) -> str:
    return f"/v1/jurisdictions/{jurisdictions.cli_name(code)}"


class Jurisdiction(BaseModel):
    model_config = _examples(_JURISDICTION_EXAMPLE)
    code: str = Field(description="ISO 3166-1 alpha-2, or a subdivision such as US-NJ")
    name: str
    url: str
    registers: list[RegisterSummary]

    @classmethod
    def of(cls, code: str, regs: list[base.Register]) -> Jurisdiction:
        return cls(code=jurisdictions.normalise(code), name=jurisdictions.name(code),
                   url=_jurisdiction_url(code), registers=[RegisterSummary.of(r) for r in regs])


class JurisdictionDetail(BaseModel):
    model_config = _examples({**_JURISDICTION_EXAMPLE, "registers": [{**_REGISTER_EXAMPLE, "freshness": None}]})
    code: str
    name: str
    url: str
    registers: list[RegisterDetail]

    @classmethod
    def of(cls, code: str, regs: list[base.Register], health: dict[str, dict[str, Any]] | None) -> JurisdictionDetail:
        return cls(code=jurisdictions.normalise(code), name=jurisdictions.name(code), url=_jurisdiction_url(code),
                   registers=[RegisterDetail.with_health(r, health) for r in regs])


class JurisdictionPage(BaseModel):
    model_config = _examples({"data": [_JURISDICTION_EXAMPLE], "pagination": {
        "next_cursor": None, "has_more": False, "limit": 20, "total": 20}})
    data: list[Jurisdiction]
    pagination: Pagination


# --- rows ------------------------------------------------------------------------

_ROW_EXAMPLE = {
    "id": 1187, "first_seen_snapshot_id": 412,
    "first_seen_at": "2026-10-05T06:01:12Z", "last_seen_at": "2026-10-07T06:00:41Z",
    "values": {"account_number": "102", "licence_number": "000102-N-000000-000", "status": "Pending",
               "type": "Non-Remote", "activity": "Bingo", "start_date": None, "end_date": None},
}


class Row(BaseModel):
    model_config = _examples(_ROW_EXAMPLE)
    id: int = Field(description="Stable for as long as the row is unchanged; a changed row is a new row "
                                "(and the old one is removed). Pages are keyed on it")
    first_seen_snapshot_id: int = Field(description="The complete snapshot that first contained this row")
    first_seen_at: datetime = Field(description="When the first complete snapshot containing this row was recorded")
    last_seen_at: datetime = Field(description="When the newest complete snapshot still containing it was recorded")
    values: dict[str, Value] = Field(description="The register's own columns, as published; "
                                                 "keys are the table's declared columns")

    @classmethod
    def of(cls, table: base.Table, row: dict[str, Any]) -> Row:
        return cls(id=row["id"], first_seen_snapshot_id=row["first_seen_snapshot_id"],
                   first_seen_at=row["first_seen_at"], last_seen_at=row["last_seen_at"],
                   values={c: row.get(c) for c in table.column_names})


class RowDetail(Row):
    model_config = _examples({**_ROW_EXAMPLE, "last_seen_snapshot_id": 431, "removed_snapshot_id": None,
                              "removed_at": None, "current": True,
                              "url": "/v1/registers/gb_ukgc/tables/licences/rows/1187"})
    last_seen_snapshot_id: int = Field(description="The newest complete snapshot that still contained it")
    removed_snapshot_id: int | None = Field(description="The first complete snapshot without it; null while current")
    removed_at: datetime | None
    current: bool = Field(description="In the register's latest complete snapshot")
    url: str

    @classmethod
    def of(cls, table: base.Table, row: dict[str, Any], *, register: str = "") -> RowDetail:
        return cls(**Row.of(table, row).model_dump(), last_seen_snapshot_id=row["last_seen_snapshot_id"],
                   removed_snapshot_id=row["removed_snapshot_id"], removed_at=row["removed_at"],
                   current=row["removed_snapshot_id"] is None,
                   url=f"/v1/registers/{register}/tables/{table.name}/rows/{row['id']}")


class RowPage(BaseModel):
    model_config = _examples({"data": [_ROW_EXAMPLE], "pagination": {
        "next_cursor": "eyJwIjp7Im8iOjF9LCJzIjoiYjk0YzQ3YjNiYzNkZTE2NyJ9", "has_more": True,
        "limit": 1, "total": 399}})
    data: list[Row]
    pagination: Pagination


# --- search ----------------------------------------------------------------------

class SearchHit(BaseModel):
    model_config = _examples({
        "jurisdiction": "CH", "register": "ch_esbk", "regulator": "Eidgenössische Spielbankenkommission (ESBK)",
        "kind": "blocklist", "table": "blocked_domains", "total": 1,
        "rows": [{"id": 77, "first_seen_snapshot_id": 410,
                  "first_seen_at": "2026-10-05T06:01:12Z", "last_seen_at": "2026-10-07T06:00:41Z",
                  "values": {"domain": "0101b00merang-bet.com", "listed_on": "2026-08-25"}}],
        "rows_url": "/v1/registers/ch_esbk/tables/blocked_domains/rows?q=b00merang&include_total=true",
    })
    jurisdiction: str
    register_: str = REGISTER
    regulator: str
    kind: base.RegisterKind
    table: str
    total: int = Field(description="Matching rows in this table")
    rows: list[Row] = Field(description="The first `limit` of them")
    rows_url: str = Field(description="Every match in this table, paged, with the same `total`")


class SearchHitPage(BaseModel):
    model_config = _examples({"data": [SearchHit.model_config["json_schema_extra"]["examples"][0]],
                              "pagination": {"next_cursor": None, "has_more": False, "limit": 1, "total": 1}})
    data: list[SearchHit] = Field(description="One entry per table with a match, most matches first")
    pagination: Pagination


# --- domains ---------------------------------------------------------------------

class DomainMatch(BaseModel):
    model_config = _examples({
        "jurisdiction": "US-NJ", "register": "us_nj_dge", "regulator": "New Jersey Division of Gaming Enforcement",
        "kind": "licensees", "table": "internet_gaming_sites", "match": "subdomain",
        "row": {"id": 9, "first_seen_snapshot_id": 405,
                "first_seen_at": "2026-10-05T06:01:12Z", "last_seen_at": "2026-10-07T06:00:41Z",
                "values": {"licensee": "HARD ROCK HOTEL AND CASINO", "site": "nj.bet365.com",
                           "host": "nj.bet365.com", "status": "authorized"}},
    })
    jurisdiction: str
    register_: str = REGISTER
    regulator: str
    kind: base.RegisterKind
    table: str
    match: Literal["exact", "subdomain"] = Field(description="`subdomain`: the row lists a subdomain of the domain")
    row: Row


class DomainStatus(BaseModel):
    model_config = _examples({
        "domain": "bet365.com", "licensed_in": ["US-NJ"], "blocked_in": ["CH"],
        "matches": [DomainMatch.model_config["json_schema_extra"]["examples"][0]],
    })
    domain: str = Field(description="The hostname checked, lower-cased, without a leading `www.`")
    licensed_in: list[str] = Field(description="Jurisdictions whose licensee registers list it")
    blocked_in: list[str] = Field(description="Jurisdictions whose blocklists list it")
    matches: list[DomainMatch]


# --- changes ---------------------------------------------------------------------

_CHANGE_EXAMPLE = {
    "change": "removed", "at": "2026-10-07T06:00:44Z", "snapshot_id": 431, "register": "gb_ukgc",
    "table": "licences", "row": {**_ROW_EXAMPLE, "values": {**_ROW_EXAMPLE["values"], "status": "Active"}},
}


class ChangeEvent(BaseModel):
    model_config = _examples(_CHANGE_EXAMPLE)
    change: Literal["added", "removed"] = Field(description="A changed row is one `removed` and one `added`")
    at: datetime = Field(description="When the snapshot that made the change was recorded")
    snapshot_id: int
    register_: str = REGISTER
    table: str
    row: Row

    @classmethod
    def of(cls, event: dict[str, Any]) -> ChangeEvent:
        return cls(change=event["change"], at=event["at"], snapshot_id=event["snapshot_id"],
                   register_=event["register"].slug, table=event["table"].name,
                   row=Row.of(event["table"], event))


class ChangePage(BaseModel):
    model_config = _examples({"data": [_CHANGE_EXAMPLE], "pagination": {
        "next_cursor": "eyJwIjp7ImsiOls0MzEsImdiX3VrZ2MiLCJsaWNlbmNlcyIsMTE4NywicmVtb3ZlZCJdfSwicyI6IjEyIn0",
        "has_more": True, "limit": 1, "total": None}})
    data: list[ChangeEvent] = Field(description="Oldest first: by snapshot, then table, row id, change")
    pagination: Pagination


# --- snapshots -------------------------------------------------------------------

_SNAPSHOT_EXAMPLE = {
    "id": 433, "register": "gb_ukgc", "run_started_at": "2026-10-07T06:00:02Z", "fetched_at": "2026-10-07T06:00:41Z",
    "complete": False, "incomplete_reason": "PAGE_GAP:3/4 licences:HTTP_503", "record_count": None,
    "http_status": 503, "pages_expected": 4, "pages_ok": 3,
}


class Snapshot(BaseModel):
    model_config = _examples(_SNAPSHOT_EXAMPLE)
    id: int
    register_: str = REGISTER
    run_started_at: datetime
    fetched_at: datetime
    complete: bool = Field(description="Only complete snapshots change the register's rows")
    incomplete_reason: str | None = Field(description="Why it was not complete; null when it was")
    record_count: int | None
    http_status: int = Field(description="The worst final status across the run's requests")
    pages_expected: int
    pages_ok: int


class SnapshotPage(BaseModel):
    model_config = _examples({"data": [_SNAPSHOT_EXAMPLE], "pagination": {
        "next_cursor": "eyJwIjp7ImsiOjQzM30sInMiOiI3In0", "has_more": True, "limit": 1, "total": None}})
    data: list[Snapshot] = Field(description="Newest first")
    pagination: Pagination


# --- ingest runs -----------------------------------------------------------------

_RUN_EXAMPLE = {
    "id": "0199c1a8-7c3e-7a52-9d1e-5b6f0c2a4e11", "status": "partial",
    "requested": {"registers": None, "jurisdiction": "ch", "force": False, "accept_count_delta": False},
    "registers": ["ch_esbk", "ch_gespa"], "created_at": "2026-10-07T06:00:00Z",
    "started_at": "2026-10-07T06:00:00Z", "finished_at": "2026-10-07T06:00:19Z", "error": None,
    "results": [{"register": "ch_esbk", "snapshot_id": 431, "complete": True, "skipped": False,
                 "unchanged": False, "reason": None, "record_count": 1532,
                 "finished_at": "2026-10-07T06:00:12Z"}],
    "not_started": ["ch_gespa"], "url": "/v1/ingest-runs/0199c1a8-7c3e-7a52-9d1e-5b6f0c2a4e11",
}


class IngestRunRequest(BaseModel):
    """Which registers to ingest: `registers`, or `jurisdiction`, or neither for all."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"jurisdiction": "ch"}, {"registers": ["gb_ukgc"], "force": True}, {}]})
    registers: list[str] | None = Field(default=None, min_length=1, max_length=100,
                                        description="Register slugs; not with `jurisdiction`")
    jurisdiction: str | None = Field(default=None, max_length=10, description="Every register of one jurisdiction")
    force: bool = Field(default=False, description="Fetch even when a good snapshot is recent")
    accept_count_delta: bool = Field(default=False, description="Accept a row count far from the last one")


class IngestRunResult(BaseModel):
    model_config = _examples(_RUN_EXAMPLE["results"][0])
    register_: str = REGISTER
    snapshot_id: int | None = Field(description="The snapshot this run recorded; null if none was")
    complete: bool
    skipped: bool = Field(description="Not fetched: a good snapshot is recent and `force` was not set")
    unchanged: bool = Field(description="Fetched, and identical to the previous snapshot")
    reason: str | None = Field(description="Why it was not complete")
    record_count: int | None
    finished_at: datetime


class IngestRun(BaseModel):
    model_config = _examples(_RUN_EXAMPLE)
    id: str
    status: Literal["queued", "running", "succeeded", "partial", "failed"] = Field(
        description="`partial`: some registers incomplete or not started; `failed`: the run itself broke")
    requested: IngestRunRequest = Field(description="The request as it was received")
    registers: list[str] = Field(description="What the request resolved to, in run order")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None = Field(description="Why the run failed, e.g. `worker lost`")
    results: list[IngestRunResult] = Field(description="One per register finished, in run order")
    not_started: list[str] = Field(description="Registers the run stopped before (a shutdown)")
    url: str

    @classmethod
    def of(cls, run: dict[str, Any]) -> IngestRun:
        return cls(id=str(run["id"]), status=run["status"], requested=IngestRunRequest(**run["requested"]),
                   registers=run["registers"], created_at=run["created_at"], started_at=run["started_at"],
                   finished_at=run["finished_at"], error=run["error"], not_started=run["not_started"],
                   results=[IngestRunResult(register_=r["slug"], **{k: r[k] for k in (
                       "snapshot_id", "complete", "skipped", "unchanged", "reason", "record_count", "finished_at")})
                       for r in run["results"]],
                   url=f"/v1/ingest-runs/{run['id']}")


class IngestRunPage(BaseModel):
    model_config = _examples({"data": [_RUN_EXAMPLE], "pagination": {
        "next_cursor": None, "has_more": False, "limit": 100, "total": None}})
    data: list[IngestRun] = Field(description="Newest first")
    pagination: Pagination


# --- status and probes -------------------------------------------------------------

class RegisterStatus(BaseModel):
    model_config = _examples({
        "slug": "ch_esbk", "jurisdiction": "CH", "stale": True, "hours_since_good": 30.2,
        "freshness": {"last_attempt_at": "2026-10-07T06:00:41Z", "last_good_at": "2026-10-06T04:47:03Z",
                      "last_reason": "PAGE_GAP:0/1 blocklist:HTTP_503", "failed_7d": 3},
        "url": "/v1/registers/ch_esbk"})
    slug: str
    jurisdiction: str
    stale: bool = Field(description="No complete snapshot within `stale_after_h` hours: an old one, none ever, "
                                    "or none known because the database cannot be reached")
    hours_since_good: float | None = Field(description="Hours since `freshness.last_good_at`; null when there is "
                                                       "no good snapshot, or it is unknown")
    freshness: Freshness | None = Field(description="Null when the database cannot be reached")
    url: str


class Status(BaseModel):
    """How fresh every register is. `stale` is the one field an uptime monitor needs."""

    model_config = _examples({
        "stale": True, "database": "ok", "stale_after_h": 26.0, "stale_registers": ["ch_esbk"],
        "checked_at": "2026-10-07T10:02:13Z",
        "registers": [RegisterStatus.model_config["json_schema_extra"]["examples"][0]]})
    stale: bool = Field(description="Any register is stale, or the database cannot be reached")
    database: Literal["ok", "unreachable"] = Field(
        description="`unreachable`: the query failed, or no answer within 2 s (the database is down, has "
                    "stopped answering, or has no connection free)")
    stale_after_h: float = Field(description="A register is stale once its newest complete snapshot is older")
    stale_registers: list[str]
    checked_at: datetime
    registers: list[RegisterStatus] = Field(description="Every register, by slug")


class Probe(BaseModel):
    model_config = _examples({"status": "ok"})
    status: Literal["ok"]
