"""France — Autorité nationale des jeux (ANJ), approved online operators.

One editorial page: each operator is an <h3> followed by a paragraph giving
"Nom du site / des sites" and "Catégorie(s)". Sites and categories are separated
by " - ". The labels drift between singular and plural and the colon moves
inside and outside the <strong>; the parser matches the words, not the markup.
"""

from __future__ import annotations

import re

from registerwatch.registers.base import Bundle, Column, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, html_doc, text

URL = "https://anj.fr/offre-de-jeu-et-marche/operateurs-agrees"
_SITES = re.compile(r"Nom d(?:u|es) sites?\s*:\s*(.*?)\s*(?:Catégories?\s*:|$)", re.I)
_CATS = re.compile(r"Catégories?\s*:\s*(.*)$", re.I)


def _entries(b: Bundle):
    doc = html_doc(b["operators"])
    box = doc.xpath("//div[contains(@class,'bloc-editotext_text')]")
    if not box:
        raise ParseError("NO_LIST", "div.bloc-editotext_text")
    operator = None
    for el in box[0]:
        if el.tag == "h3":
            operator = text(el) or None
            continue
        t = text(el) or ""
        m = _SITES.search(t)
        if el.tag == "p" and operator and m:
            sites = [s for s in (clean(x) for x in re.split(r"\s+-\s+|,\s*", m.group(1))) if s]
            c = _CATS.search(t)
            cats = [x for x in (clean(y) for y in re.split(r"\s+-\s+", c.group(1))) if x] if c else []
            yield operator, sites, cats
            operator = None


def _operators(b: Bundle):
    return [{"operator": op, "categories": cats} for op, _, cats in _entries(b)]


def _websites(b: Bundle):
    return [{"operator": op, "website": s, "host": host(s)} for op, sites, _ in _entries(b) for s in sites]


REGISTER = Register(
    slug="fr_anj",
    name="Approved online gambling operators",
    regulator="Autorité nationale des jeux (ANJ)",
    country="FR",
    kind="licensees",
    homepage=URL,
    parts=(Part("operators", URL, "html"),),
    tables=(
        Table("operators", (Column("operator", required=True), Column("categories", "text[]")), _operators,
              min_rows=5),
        Table("websites", (Column("operator", required=True), Column("website", required=True),
                           Column("host")), _websites, min_rows=5),
    ),
)
