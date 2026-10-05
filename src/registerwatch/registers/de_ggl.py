"""Germany — Gemeinsame Glücksspielbehörde der Länder (GGL), the whitelist.

One server-rendered page lists every permitted operator (~170) as an accordion
item. Each item holds one block per permit — gambling type, address, channel,
sales area, competent authority — and the websites that permit covers, with
first-permit and follow-up permit dates. The page also embeds a second, PDF-
oriented copy of every item; the parser reads only the accordion content.
"""

from __future__ import annotations

import re

from registerwatch.registers.base import Bundle, Column, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, html_doc, text, to_date

URL = "https://www.gluecksspiel-behoerde.de/de/fuer-spielende/uebersicht-erlaubter-anbieter-whitelist"

_FIELDS = {"Adresse": "address", "Vertriebsweg": "distribution_channel",
           "Vertriebsgebiet": "sales_area", "Zuständigkeit": "authority", "Standorte": "locations"}


def _items(b: Bundle):
    doc = html_doc(b["whitelist"])
    items = doc.xpath("//ul[@id='gglwhitelist-accordion']/li")
    if not items:
        raise ParseError("NO_ACCORDION", "ul#gglwhitelist-accordion")
    for li in items:
        title = li.xpath("./a[contains(@class,'uk-accordion-title')]")
        content = li.xpath("./div[contains(@class,'uk-accordion-content')]")
        if not title or not content:
            raise ParseError("ITEM_SHAPE", (text(li) or "")[:80])
        yield text(title[0]), content[0].xpath("./div[contains(@class,'uk-grid')]")


def _cell_lines(td) -> str | None:
    parts = [clean(s) for s in td.xpath(".//text()")]
    return ", ".join(p for p in parts if p) or None


def _permits(b: Bundle):
    rows = []
    for operator, blocks in _items(b):
        for block in blocks:
            left = block.xpath(".//div[contains(@class,'left')]")
            if not left:
                continue
            row = {"operator": operator, "gambling_type": text(left[0].xpath(".//h3")[0]) if left[0].xpath(".//h3") else None}
            for tr in left[0].xpath(".//tr"):
                th, td = tr.xpath("./th"), tr.xpath("./td")
                if not th or not td:
                    continue
                label = re.sub(r"^\d+\.\s*", "", (text(th[0]) or "").rstrip(":"))
                col = _FIELDS.get(label)
                if col:
                    value = _cell_lines(td[0])
                    row[col] = f"{row[col]} | {value}" if row.get(col) and value else (row.get(col) or value)
            rows.append(row)
    return rows


def _websites(b: Bundle):
    rows = []
    for operator, blocks in _items(b):
        for block in blocks:
            h3 = block.xpath(".//div[contains(@class,'left')]//h3")
            gtype = text(h3[0]) if h3 else None
            for site in block.xpath(".//div[contains(@class,'right')]//div[contains(@class,'el-title')]/.."):
                name = text(site.xpath(".//div[contains(@class,'el-title')]")[0])
                meta = text(site.xpath(".//div[contains(@class,'el-meta')]")[0]) if site.xpath(".//div[contains(@class,'el-meta')]") else ""
                first = re.search(r"Erstmaliges Erlaubnisdatum:\s*(\d{2}\.\d{2}\.\d{4})", meta or "")
                renew = re.search(r"Folgeerlaubnisdatum:\s*(\d{2}\.\d{2}\.\d{4})", meta or "")
                rows.append({"operator": operator, "gambling_type": gtype, "website": name, "host": host(name),
                             "first_permit_date": to_date(first.group(1)) if first else None,
                             "renewal_date": to_date(renew.group(1)) if renew else None})
    return rows


REGISTER = Register(
    slug="de_ggl",
    name="Whitelist of permitted gambling operators",
    regulator="Gemeinsame Glücksspielbehörde der Länder (GGL)",
    country="DE",
    kind="licensees",
    homepage=URL,
    parts=(Part("whitelist", URL, "html"),),
    tables=(
        Table("permits", (Column("operator", required=True), Column("gambling_type", required=True),
                          Column("address"), Column("distribution_channel"), Column("sales_area"),
                          Column("authority"), Column("locations")), _permits,
              description="One row per operator and gambling-type permit"),
        Table("websites", (Column("operator", required=True), Column("gambling_type"),
                           Column("website", required=True), Column("host"),
                           Column("first_permit_date", "date"), Column("renewal_date", "date")), _websites,
              min_rows=10, description="Websites covered by each permit"),
    ),
)
