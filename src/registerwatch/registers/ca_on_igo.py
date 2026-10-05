"""Ontario — iGaming Ontario, the regulated iGaming market.

One page listing every operator brand live in Ontario's regulated market:
brand, website and offerings (casino, sports, poker…).
"""

from __future__ import annotations

from registerwatch.registers.base import Bundle, Column, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, html_doc, text

URL = "https://igamingontario.ca/en/player/regulated-igaming-market"


def _brands(b: Bundle):
    doc = html_doc(b["market"])
    items = doc.xpath("//li[contains(@class,'operator-item')]")
    if not items:
        raise ParseError("NO_ITEMS", "li.operator-item")
    rows = []
    for li in items:
        a = li.xpath(".//div[contains(@class,'views-field-field-logo')]//a")
        title = li.xpath(".//div[contains(@class,'views-field-title')]")
        site = clean(a[0].get("href")) if a else None
        brand = clean(a[0].get("title")) if a and a[0].get("title") else (text(title[0]) if title else None)
        rows.append({"brand": brand, "website": site, "host": host(site),
                     "offerings": [text(x) for x in li.xpath(".//div[contains(@class,'views-field-field-offerings')]//li")],
                     "link_text": text(title[0]) if title else None})
    return rows


REGISTER = Register(
    slug="ca_on_igo",
    name="Regulated iGaming market — operator brands",
    regulator="iGaming Ontario",
    country="CA-ON",
    kind="licensees",
    homepage=URL,
    parts=(Part("market", URL, "html"),),
    tables=(
        Table("brands", (Column("brand", required=True), Column("website", required=True), Column("host"),
                         Column("offerings", "text[]"), Column("link_text")), _brands, min_rows=20),
    ),
)
