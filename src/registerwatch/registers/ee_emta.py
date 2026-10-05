"""Estonia — Maksu- ja Tolliamet (EMTA), list of legal gambling operators.

One page, one accordion per gambling subtype (online, casino, ship, toto, games
of skill, lotteries), each holding a table: operator, brand, website(s), and a
link to the operator's record in the Register of Economic Activities (MTR).
"""

from __future__ import annotations

import re

from registerwatch.registers.base import Bundle, Column, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, html_doc, text

URL = "https://www.emta.ee/en/business-client/registration-business/gambling-operators/list-legal-gambling-operators"
HEADER = ["Gambling operator", "Brand", "Website", "Licence information"]


def _rows(b: Bundle):
    doc = html_doc(b["operators"])
    tables = doc.xpath("//table")
    if not tables:
        raise ParseError("NO_TABLE")
    for tb in tables:
        trs = tb.xpath(".//tr")
        if not trs or [text(c) for c in trs[0].xpath("./th|./td")] != HEADER:
            raise ParseError("HEADER_MISMATCH", "|".join(text(c) or "" for c in trs[0].xpath("./th|./td")) if trs else "")
        box = tb.xpath("ancestor::*[starts-with(@aria-labelledby,'accordion-title--')][1]/@aria-labelledby")
        subtype = box[0].removeprefix("accordion-title--") if box else None
        for tr in trs[1:]:
            cells = tr.xpath("./td")
            if len(cells) < 4 or not text(cells[0]):
                continue
            sites = [clean(a.text_content()) for a in cells[2].xpath(".//a")] or \
                    [s for s in re.split(r"\s+", text(cells[2]) or "") if s]
            mtr = cells[3].xpath(".//a/@href")
            yield subtype, text(cells[0]), text(cells[1]), [s for s in sites if s], clean(mtr[0]) if mtr else None


def _operators(b: Bundle):
    return [{"subtype": st, "operator": op, "brand": br, "register_url": mtr}
            for st, op, br, _, mtr in _rows(b)]


def _websites(b: Bundle):
    return [{"subtype": st, "operator": op, "website": s, "host": host(s)}
            for st, op, _, sites, _ in _rows(b) for s in sites]


REGISTER = Register(
    slug="ee_emta",
    name="List of legal gambling operators",
    regulator="Maksu- ja Tolliamet (Estonian Tax and Customs Board)",
    country="EE",
    kind="licensees",
    homepage=URL,
    parts=(Part("operators", URL, "html"),),
    tables=(
        Table("operators", (Column("subtype", required=True), Column("operator", required=True),
                            Column("brand"), Column("register_url")), _operators, min_rows=10,
              known_values={"subtype": frozenset({"online", "casino", "ship", "toto", "games-skill", "lotteries"})}),
        Table("websites", (Column("subtype"), Column("operator", required=True), Column("website", required=True),
                           Column("host")), _websites, min_rows=10),
    ),
)
