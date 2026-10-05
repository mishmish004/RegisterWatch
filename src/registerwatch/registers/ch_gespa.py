"""Switzerland — Gespa (intercantonal authority: lotteries, sports betting), blocklist.

blocklist.gespa.ch is a plain directory of dated, signed releases:
gespa_blocklist_YYYYMMDD.{txt,pdf} with .sign files. The plan takes the newest
TXT. Lines starting with '#' are a version/serial header.
"""

from __future__ import annotations

import re

from registerwatch.registers.base import Bundle, Column, Fetch, Part, Register, Table
from registerwatch.registers.extract import dedupe, is_domain, text_lines

INDEX = "https://blocklist.gespa.ch/"
_TXT = re.compile(r'href="(gespa_blocklist_(\d{8})\.txt)"')


def plan(fetch: Fetch) -> list[Part]:
    found = sorted(_TXT.findall(fetch(INDEX).decode("utf-8", "replace")), key=lambda m: m[1])
    return [Part("blocklist", INDEX + found[-1][0] if found else None, "txt")]


def _domains(b: Bundle):
    return dedupe(({"domain": d.lower(), "well_formed": is_domain(d.lower())}
                   for d in text_lines(b["blocklist"])), ["domain"])


REGISTER = Register(
    slug="ch_gespa",
    name="Blocklist of unauthorised lottery and betting sites",
    regulator="Gespa — Interkantonale Geldspielaufsicht",
    country="CH",
    kind="blocklist",
    homepage="https://www.gespa.ch/en/fighting-illegal-gambling/access-blocking",
    plan=plan,
    tables=(
        Table("blocked_domains", (Column("domain", required=True), Column("well_formed", "bool")), _domains,
              min_rows=100),
    ),
)
