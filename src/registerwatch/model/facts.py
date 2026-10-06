"""The five fact kinds, and the vocabularies that make registers comparable.

A projection (projections.py) turns each register row into facts. A fact is

  kind    party | licence | brand | domain | block
  key     its identity within the register and kind. Two rows with the same
          key are versions of one thing: UKGC renumbers a licence by bumping
          the last segment of its number, and the key leaves that segment out,
          so the renumbering is a change to one licence rather than one revoked
          and another granted
  values  what the register says about it (FIELDS[kind]); versions are
          compared on these, so a cosmetic change to a row that does not touch
          a value is not a change at all
  ref     the register row it came from: table, row id, and the snapshots that
          first saw it and removed it. This is the evidence.

Links between facts (a licence's party, a domain's licence) are keys within the
same register; build.py turns them into model ids.

Statuses and products are normalised to small vocabularies so "Aktiv", "Active"
and "Licenced" can be filtered as one. The published value is always kept next
to the normalised one (status_raw), and a value nobody mapped is "unknown", not
a guess.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from registerwatch.registers.extract import to_date

Kind = Literal["party", "licence", "brand", "domain", "block"]
KINDS: tuple[Kind, ...] = ("party", "licence", "brand", "domain", "block")

FIELDS: dict[str, tuple[str, ...]] = {
    "party": ("name", "identifiers"),
    "licence": ("party", "reference", "type", "products", "channel", "area", "site", "status_raw", "valid_from",
                "valid_to", "authority", "notes"),
    "brand": ("party", "name", "status_raw", "products"),
    "domain": ("party", "brand", "licence", "host", "published", "status_raw", "products", "since"),
    "block": ("host", "published", "listed_on"),
}


@dataclass(frozen=True)
class RowRef:
    """One row of a register table, with its place in the row history."""
    table: str
    row_id: int
    first_snapshot: int
    removed_snapshot: int | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    removed_at: datetime | None

    @property
    def current(self) -> bool:
        return self.removed_snapshot is None


@dataclass(frozen=True)
class Fact:
    kind: str
    key: str
    values: tuple[tuple[str, Any], ...]   # FIELDS[kind] order; lists frozen to tuples
    ref: RowRef

    def get(self, name: str) -> Any:
        return dict(self.values).get(name)

    def as_dict(self) -> dict[str, Any]:
        return {k: _thaw(v) for k, v in self.values}


def _make(kind: str, ref: RowRef, key: str, values: Mapping[str, Any]) -> Fact:
    unknown = set(values) - set(FIELDS[kind])
    if unknown:
        raise ValueError(f"{kind}: undeclared fields {sorted(unknown)}")
    return Fact(kind, key, tuple((f, _freeze(values.get(f))) for f in FIELDS[kind]), ref)


def party(ref: RowRef, key: str, name: str | None, **identifiers: str | None) -> Fact:
    ids = {k: v for k, v in identifiers.items() if v}
    return _make("party", ref, key, {"name": name, "identifiers": ids})


def licence(ref: RowRef, key: str, **values: Any) -> Fact:
    return _make("licence", ref, key, values)


def brand(ref: RowRef, key: str, **values: Any) -> Fact:
    return _make("brand", ref, key, values)


def domain(ref: RowRef, key: str, **values: Any) -> Fact:
    return _make("domain", ref, key, values)


def block(ref: RowRef, key: str, **values: Any) -> Fact:
    return _make("block", ref, key, values)


class FrozenMap(tuple):
    """A mapping frozen to sorted (key, value) pairs: hashable, so versions can
    be compared as sets, and still told apart from a frozen list on the way out."""


def _freeze(v: Any) -> Any:
    if isinstance(v, Mapping):
        return FrozenMap(sorted((k, _freeze(x)) for k, x in v.items()))
    if isinstance(v, (list, tuple)):
        return tuple(_freeze(x) for x in v)
    return v


def _thaw(v: Any) -> Any:
    if isinstance(v, FrozenMap):
        return {k: _thaw(x) for k, x in v}
    if isinstance(v, tuple):
        return [_thaw(x) for x in v]
    return v


def thaw(v: Any) -> Any:
    return _thaw(v)


# --- statuses -----------------------------------------------------------------------

Status = Literal["active", "pending", "suspended", "revoked", "surrendered", "expired", "lapsed",
                 "forfeited", "inactive", "white_label", "listed", "unknown"]

# What a holder can do now. "listed" is a register that states no status: being
# on a list of permitted operators is the status.
OPERATING = frozenset({"active", "listed", "white_label"})
NOT_OPERATING = frozenset({"suspended", "revoked", "surrendered", "expired", "lapsed", "forfeited", "inactive"})

_STATUS = {
    "active": "active", "aktiv": "active", "licenced": "active", "licensed": "active",
    "authorized": "active", "authorised": "active", "current": "active",
    "inactive": "inactive", "inaktiv": "inactive",
    "pending": "pending",
    "suspended": "suspended",
    "revoked": "revoked", "revoked - non payment of fee": "revoked",
    "surrendered": "surrendered", "expired": "expired", "lapsed": "lapsed", "forfeited": "forfeited",
    "white label": "white_label",
}
# "Suspended (2 October 2026)", "Suspended (02/10/2026)": the Isle of Man dates its statuses.
_DATED = re.compile(r"^(?P<word>[^()]+?)\s*\((?P<when>[^()]+)\)\s*$")


def status(raw: str | None) -> tuple[str, date | None]:
    """Published status -> (normalised status, date it took effect if stated)."""
    if raw is None:
        return "listed", None
    s = raw.strip()
    m = _DATED.match(s)
    since = None
    if m:
        s, since = m.group("word"), to_date(m.group("when"))
    return _STATUS.get(s.casefold(), "unknown"), since


# --- products ---------------------------------------------------------------------------

Product = Literal["casino", "betting", "horse_racing", "poker", "bingo", "lottery", "gaming_machines", "b2b"]
PRODUCTS: tuple[str, ...] = ("casino", "betting", "horse_racing", "poker", "bingo", "lottery", "gaming_machines",
                             "b2b")

# Matched against the accent-free, casefolded text of a licence type, activity,
# category or offering, in any of the registers' languages. A register whose
# terms these do not cover gets explicit products in its projection instead.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("b2b", ("software", "technical", "supplier", "supply", "network services", "spelprogramvara",
             "equipment")),
    ("horse_racing", ("horse", "hippique", "pferd", "dostih", "racing")),
    ("betting", ("betting", "wett", "paris sportifs", "paris hippiques", "apuesta", "scommess", "vadhallning",
                 "stavk", "sazk", "bookmaker", "poolspel", "betting exchange")),
    ("poker", ("poker", "jeux de cercle", "kortspel")),
    ("bingo", ("bingo",)),
    ("lottery", ("lotter", "lotto", "loteri", "keno", "spielvermittlung")),
    ("casino", ("casino", "kasino", "kasin", "spielbank", "automatenspiel", "zive hry")),
    ("gaming_machines", ("gaming machine", "slot machine", "vyhernych pristroj", "videohier", "vardeautomat",
                         "amusement")),
)


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", text)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def products(*texts: str | None) -> tuple[str, ...]:
    """Product tags named by any of `texts`, in PRODUCTS order; () if none."""
    folded = " | ".join(_fold(t) for t in texts if t)
    found = {tag for tag, words in _KEYWORDS if any(w in folded for w in words)}
    return tuple(p for p in PRODUCTS if p in found)


def merge_products(*groups: Iterable[str]) -> tuple[str, ...]:
    found = {p for g in groups for p in g}
    return tuple(p for p in PRODUCTS if p in found)


# --- channels -------------------------------------------------------------------------

Channel = Literal["online", "land", "both"]


def channel(raw: str | None) -> str | None:
    """"Remote", "Internet", "online" -> online; "Non-Remote", "Land-Based",
    "stationär" -> land; "Hybrid", "online/stationär" -> both."""
    if not raw:
        return None
    s = _fold(raw)
    online = any(w in s for w in ("online", "internet", "i-kasin")) or (
        "remote" in s and "non-remote" not in s)
    land = any(w in s for w in ("stationar", "land", "non-remote"))
    if "hybrid" in s or (online and land):
        return "both"
    return "online" if online else ("land" if land else None)
