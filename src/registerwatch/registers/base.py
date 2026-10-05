"""What a register declares, and nothing about how it is fetched or stored.

A register is a module under registers/ that builds one `Register`:

  parts    what to download. Either fixed URLs, or a `plan` that reads an index
           page first (discovery) and returns the URLs it found. A part the
           register *expects* but cannot find is still returned, with url=None,
           so its absence is recorded as NOT_LISTED instead of silently
           shrinking the snapshot.
  tables   what the downloaded bytes mean. Each table has typed columns and a
           pure `parse(bundle) -> rows` function. The engine creates one
           Postgres schema per register (named after the slug) with one table
           per `Table`, and keeps row history there.

Parsers are pure: bytes in, rows out, no network and no database. That is what
lets the same function run in the daily ingest, in a re-parse of old blobs, and
in a test against a saved fixture.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

ColumnType = Literal["text", "date", "timestamptz", "int", "bigint", "numeric", "bool", "text[]"]
PartKind = Literal["csv", "html", "json", "xml", "xlsx", "txt", "pdf"]
RegisterKind = Literal["licensees", "blocklist"]

_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class ParseError(ValueError):
    """The bytes are not the shape this parser was written for.

    `code` is short and stable (HEADER_MISMATCH, NO_TABLE, RAGGED_ROWS…) because
    it ends up in incomplete_reason, where people grep for it.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


class DiscoveryError(RuntimeError):
    """An index page needed to find the data could not be fetched or read."""

    def __init__(self, url: str, status: int | None, detail: str = "") -> None:
        super().__init__(f"{url}: {status} {detail}".strip())
        self.url = url
        self.status = status


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType = "text"
    # A required column must be non-empty in (nearly) every row. Most rows
    # missing it means the layout moved and the parser is reading the wrong cell.
    required: bool = False

    def __post_init__(self) -> None:
        if not _IDENT.match(self.name):
            raise ValueError(f"column name {self.name!r} is not a safe SQL identifier")


@dataclass(frozen=True)
class Part:
    name: str                 # stable key: "licences", "page-003", "class-bplus"
    url: str | None           # None = expected but not advertised (NOT_LISTED)
    kind: PartKind
    expected: bool = True     # counts towards completeness


class Bundle(Mapping[str, bytes]):
    """The downloaded parts of one run, by part name."""

    def __init__(self, parts: Mapping[str, bytes]) -> None:
        self._parts = dict(parts)

    def __getitem__(self, name: str) -> bytes:
        try:
            return self._parts[name]
        except KeyError:
            raise ParseError("MISSING_PART", name) from None

    def __iter__(self):
        return iter(self._parts)

    def __len__(self) -> int:
        return len(self._parts)

    def prefixed(self, prefix: str) -> list[tuple[str, bytes]]:
        """Parts whose name starts with `prefix`, in name order (pages, classes)."""
        return sorted((k, v) for k, v in self._parts.items() if k.startswith(prefix))


Row = dict[str, Any]
Fetch = Callable[[str], bytes]  # discovery: GET a URL through the engine, logged


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    parse: Callable[[Bundle], list[Row]]
    min_rows: int = 1
    # Values seen in, or documented for, a column. Anything else is a warning:
    # a new status is a real change the publisher made, but someone should
    # decide what it means before a differ does.
    known_values: Mapping[str, frozenset[str]] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        if not _IDENT.match(self.name):
            raise ValueError(f"table name {self.name!r} is not a safe SQL identifier")
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.name}: duplicate column names")

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass(frozen=True)
class Register:
    slug: str                 # also the Postgres schema name
    name: str                 # "Belgian Gaming Commission — licence registers"
    regulator: str
    country: str              # ISO 3166-1 alpha-2, or a subdivision code (US-NJ, CA-QC)
    kind: RegisterKind
    homepage: str             # the human-readable page this data backs
    tables: tuple[Table, ...]
    # Either fixed parts, or a plan that may fetch index pages first.
    parts: tuple[Part, ...] = ()
    plan: Callable[[Fetch], Sequence[Part]] | None = None
    min_interval_ms: int = 3000
    timeout_s: float | None = None   # per-request; None = HTTP_TIMEOUT_S
    count_delta_tolerance: float = 0.10
    # PEM file (under registers/certs/) with an intermediate the server forgets
    # to send. Verification stays on; the chain is completed, not skipped.
    extra_ca: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not _IDENT.match(self.slug):
            raise ValueError(f"slug {self.slug!r} is not a safe schema name")
        if not self.parts and self.plan is None:
            raise ValueError(f"{self.slug}: needs parts or a plan")
        if self.slug in {"public", "cron", "net", "vault", "auth", "storage", "extensions"}:
            raise ValueError(f"{self.slug}: reserved schema name")

    def resolve_parts(self, fetch: Fetch) -> list[Part]:
        parts = list(self.plan(fetch)) if self.plan else list(self.parts)
        names = [p.name for p in parts]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.slug}: duplicate part names {names}")
        return parts

    def config(self) -> dict[str, Any]:
        return {
            "regulator": self.regulator,
            "country": self.country,
            "kind": self.kind,
            "homepage": self.homepage,
            "schema": self.slug,
            "tables": {t.name: t.column_names for t in self.tables},
            "parts": [p.name for p in self.parts] if self.parts else "discovered",
            "count_delta_tolerance": self.count_delta_tolerance,
        }
