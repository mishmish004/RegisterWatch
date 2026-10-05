"""New Jersey — Division of Gaming Enforcement, authorised internet gaming sites.

One page, one table: a heading row per Atlantic City licensee (upper case,
single cell) followed by numbered rows of the sites it is authorised to run,
each marked with a green check image. A row can list several sites, comma-
separated; each becomes its own row.
"""

from __future__ import annotations

import re

from registerwatch.registers.base import Bundle, Column, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, html_doc, text

URL = "https://www.njoag.gov/about/divisions-and-offices/division-of-gaming-enforcement-home/internet-gaming-sites/"


def _sites(b: Bundle):
    doc = html_doc(b["sites"])
    trs = doc.xpath("//table//tr")
    if not trs:
        raise ParseError("NO_TABLE")
    rows, licensee = [], None
    for tr in trs:
        cells = tr.xpath("./td")
        values = [text(c) for c in cells]
        if len(cells) == 1 and values[0] and values[0] == values[0].upper() and len(values[0]) > 3:
            licensee = values[0]
            continue
        if len(cells) >= 3 and values[0] and re.match(r"^\d+\.$", values[0]) and values[2]:
            imgs = " ".join(tr.xpath(".//img/@src"))
            status = "authorized" if "checkmark_green" in imgs else ("unknown" if not imgs else imgs.rsplit("/", 1)[-1])
            for site in (clean(s) for s in values[2].split(",")):
                if site:
                    rows.append({"licensee": licensee, "site": site, "host": host(site), "status": status})
    return rows


REGISTER = Register(
    slug="us_nj_dge",
    name="Internet gaming sites authorised to operate",
    regulator="New Jersey Division of Gaming Enforcement",
    country="US-NJ",
    kind="licensees",
    homepage=URL,
    parts=(Part("sites", URL, "html"),),
    tables=(
        Table("internet_gaming_sites", (Column("licensee", required=True), Column("site", required=True),
                                        Column("host"), Column("status")), _sites, min_rows=10,
              known_values={"status": frozenset({"authorized"})}),
    ),
)
