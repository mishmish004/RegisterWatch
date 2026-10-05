"""Spain — Dirección General de Ordenación del Juego (DGOJ), licensed operators.

A paginated server-rendered table (operator, list of domains), ~10 rows a page.
The plan reads page 0 to learn the last page from the pager, then fetches every
page; page 0 is not fetched twice. If the pager vanishes the run sees one page,
and the row-count check against the last good run holds the snapshot.
"""

from __future__ import annotations

import re

from registerwatch.registers.base import Bundle, Column, Fetch, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, html_doc, links, text

BASE = "https://www.ordenacionjuego.es/operadores-juego/operadores-licencia/operadores"
MAX_PAGES = 100  # runaway guard


def plan(fetch: Fetch) -> list[Part]:
    first = fetch(BASE)
    pages = [int(n) for n in re.findall(rb"[?&]page=(\d+)", first)]
    last = min(max(pages, default=0), MAX_PAGES)
    return [Part(f"page-{i:03d}", BASE if i == 0 else f"{BASE}?page={i}", "html") for i in range(last + 1)]


def _rows(b: Bundle):
    for name, raw in b.prefixed("page-"):
        doc = html_doc(raw)
        trs = doc.xpath("//table//tr[td]")
        if not trs:
            raise ParseError("NO_TABLE", name)
        for tr in trs:
            title = tr.xpath("./td[contains(@class,'views-field-title')]")
            sites = tr.xpath("./td[contains(@class,'views-field-field-links')]")
            if not title:
                raise ParseError("ROW_SHAPE", name)
            a = title[0].xpath(".//a/@href")
            yield text(title[0]), clean(a[0]) if a else None, (links(sites[0]) if sites else [])


def _operators(b: Bundle):
    return [{"operator": op, "detail_url": f"https://www.ordenacionjuego.es{href}" if href and href.startswith("/") else href}
            for op, href, _ in _rows(b)]


def _websites(b: Bundle):
    return [{"operator": op, "website": s, "host": host(s)} for op, _, sites in _rows(b) for s in sites]


REGISTER = Register(
    slug="es_dgoj",
    name="Operators with a licence for online gambling",
    regulator="Dirección General de Ordenación del Juego (DGOJ)",
    country="ES",
    kind="licensees",
    homepage=BASE,
    plan=plan,
    tables=(
        Table("operators", (Column("operator", required=True), Column("detail_url")), _operators),
        Table("websites", (Column("operator", required=True), Column("website", required=True),
                           Column("host")), _websites),
    ),
)
