"""Belgium — Kansspelcommissie / Commission des jeux de hasard.

data.gamingcommission.be serves a plain directory listing: one folder per
licence class, each with a `latest/rows.json` regenerated daily around 04:30
CET, plus dated folders going back to 2020. The JSON is what the Commission's
own table viewer renders, so it is the register, not a copy of it.

Two shapes: the "+" classes (A+, B+, F1+ = online, here APLUS/BPLUS/FAPLUS)
carry a website and an operator; the physical classes carry an owner and an
address. Each class is a part; a class folder appearing that is not listed below
is fetched as an unexpected part and lands in the table its shape matches.
"""

from __future__ import annotations

import re

from registerwatch.registers.base import Bundle, Column, Fetch, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, json_load

ROOT = "https://data.gamingcommission.be/licenses/"
EXPECTED_CLASSES = ("A", "APLUS", "B", "BPLUS", "C", "E", "FA", "FAPLUS", "FB", "FC", "FD", "FE", "GA")
ONLINE_KEYS = {"website", "operator"}


def _url(cls: str) -> str:
    return f"{ROOT}{cls}/latest/rows.json"


def plan(fetch: Fetch) -> list[Part]:
    listing = fetch(ROOT).decode("utf-8", "replace")
    present = set(re.findall(r'href="([A-Z0-9_]+)/"', listing))
    parts = [Part(f"class-{c.lower()}", _url(c) if c in present else None, "json") for c in EXPECTED_CLASSES]
    parts += [Part(f"class-{c.lower()}", _url(c), "json", expected=False)
              for c in sorted(present - set(EXPECTED_CLASSES))]
    return parts


def _records(b: Bundle):
    for name, raw in b.prefixed("class-"):
        doc = json_load(raw)
        if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
            raise ParseError("UNEXPECTED_JSON", name)
        cls = name.removeprefix("class-").upper()
        for rec in doc["data"]:
            yield cls, rec


def _online(b: Bundle):
    rows = []
    for cls, r in _records(b):
        if not ONLINE_KEYS & set(r):
            continue
        rows.append({
            "licence_class": cls, "dossier_id": clean(r.get("dossierId")),
            "establishment": clean(r.get("establishment")), "operator": clean(r.get("operator")),
            "website": clean(r.get("website")), "host": host(r.get("website")),
            "decision_date": r.get("decisionDate"), "publication_date": r.get("publicationDate"),
            "expiration_date": r.get("expirationDate"),
        })
    return rows


def _establishments(b: Bundle):
    rows = []
    for cls, r in _records(b):
        if ONLINE_KEYS & set(r):
            continue
        rows.append({
            "licence_class": cls, "dossier_id": clean(r.get("dossierId")),
            "establishment": clean(r.get("establishment")), "owner": clean(r.get("owner")),
            "street_address_nl": clean(r.get("streetAddressNl")),
            "street_address_fr": clean(r.get("streetAddressFr")),
            "postal_code": clean(r.get("postalCode")), "commune": clean(r.get("commune")),
            "province": clean(r.get("province")),
            "decision_date": r.get("decisionDate"), "publication_date": r.get("publicationDate"),
            "expiration_date": r.get("expirationDate"),
        })
    return rows


_DATES = (Column("decision_date", "date"), Column("publication_date", "date"), Column("expiration_date", "date"))

REGISTER = Register(
    slug="be_gc",
    name="Licence registers (all classes)",
    regulator="Kansspelcommissie / Commission des jeux de hasard",
    country="BE",
    kind="licensees",
    homepage="https://www.gamingcommission.be/",
    plan=plan,
    min_interval_ms=1500,
    tables=(
        Table("online_licences",
              (Column("licence_class", required=True), Column("dossier_id", required=True),
               Column("establishment"), Column("operator", required=True), Column("website", required=True),
               Column("host"), *_DATES),
              _online, description="A+, B+ and F1+ licences: one row per licensed website"),
        Table("establishments",
              (Column("licence_class", required=True), Column("dossier_id", required=True),
               Column("establishment"), Column("owner", required=True), Column("street_address_nl"),
               Column("street_address_fr"), Column("postal_code"), Column("commune"), Column("province"),
               *_DATES),
              _establishments, description="Physical licences: casinos, arcades, betting shops, cafés…"),
    ),
)
