"""Switzerland — ESBK (Federal Gaming Board: casinos), blocklist.

Published only as a PDF ("sperrliste-YYYY-MM-DD-dfi.pdf", German/French/
Italian), quarterly. Each listing line is "<domain> <dd.mm.yyyy>": the domain and
the date it was added. The plan takes the newest PDF linked from the page.
"""

from __future__ import annotations

import re

from registerwatch.registers.base import Bundle, Column, Fetch, ParseError, Part, Register, Table
from registerwatch.registers.extract import dedupe, pdf_text, to_date

PAGE = "https://www.esbk.admin.ch/en/unauthorised-online-games"
_PDF = re.compile(r'href="(https://www\.esbk\.admin\.ch/dam/[^"]*sperrliste-(\d{4}-\d{2}-\d{2})-dfi\.pdf)"', re.I)
_LINE = re.compile(r"^((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})\s+(\d{2}\.\d{2}\.\d{4})\s*$",
                   re.M | re.I)


def plan(fetch: Fetch) -> list[Part]:
    found = sorted(_PDF.findall(fetch(PAGE).decode("utf-8", "replace")), key=lambda m: m[1])
    return [Part("blocklist", found[-1][0] if found else None, "pdf")]


def _domains(b: Bundle):
    rows = [{"domain": m.group(1).lower(), "listed_on": to_date(m.group(2))}
            for m in _LINE.finditer(pdf_text(b["blocklist"]))]
    if not rows:
        raise ParseError("NO_LISTING_LINES")
    return dedupe(rows, ["domain"])


REGISTER = Register(
    slug="ch_esbk",
    name="Blocklist of unauthorised online casino games",
    regulator="Eidgenössische Spielbankenkommission (ESBK)",
    country="CH",
    kind="blocklist",
    homepage=PAGE,
    plan=plan,
    tables=(
        Table("blocked_domains", (Column("domain", required=True), Column("listed_on", "date")), _domains,
              min_rows=500),
    ),
)
