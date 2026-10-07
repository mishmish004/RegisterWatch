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


# --- errors (until Problem Details replace them: plan.md Phase 3) -----------------

class HTTPError(BaseModel):
    model_config = _examples({"detail": "unknown register 'xx_nope'; known: au_acma, be_gc, ..."})
    detail: str


NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": HTTPError, "description": "Unknown identifier"}}
BAD_REQUEST: dict[int | str, dict[str, Any]] = {400: {"model": HTTPError, "description": "Invalid parameter"}}


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
    "first_seen_at": "2026-10-05T06:01:12Z", "last_seen_at": "2026-10-07T06:00:41Z",
    "values": {"account_number": "102", "licence_number": "000102-N-000000-000", "status": "Pending",
               "type": "Non-Remote", "activity": "Bingo", "start_date": None, "end_date": None},
}


class Row(BaseModel):
    model_config = _examples(_ROW_EXAMPLE)
    first_seen_at: datetime = Field(description="When the first complete snapshot containing this row was recorded")
    last_seen_at: datetime = Field(description="When the newest complete snapshot still containing it was recorded")
    values: dict[str, Value] = Field(description="The register's own columns, as published; "
                                                 "keys are the table's declared columns")

    @classmethod
    def of(cls, table: base.Table, row: dict[str, Any]) -> Row:
        return cls(first_seen_at=row["first_seen_at"], last_seen_at=row["last_seen_at"],
                   values={c: row.get(c) for c in table.column_names})


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
        "rows": [{"first_seen_at": "2026-10-05T06:01:12Z", "last_seen_at": "2026-10-07T06:00:41Z",
                  "values": {"domain": "0101b00merang-bet.com", "listed_on": "2026-08-25"}}],
        "rows_url": "/v1/registers/ch_esbk/tables/blocked_domains/rows?q=b00merang",
    })
    jurisdiction: str
    register_: str = REGISTER
    regulator: str
    kind: base.RegisterKind
    table: str
    total: int = Field(description="Matching rows in this table")
    rows: list[Row] = Field(description="The first `limit` of them")
    rows_url: str = Field(description="Every match in this table, paged")


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
        "row": {"first_seen_at": "2026-10-05T06:01:12Z", "last_seen_at": "2026-10-07T06:00:41Z",
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
