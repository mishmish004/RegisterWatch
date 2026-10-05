"""Italy — Agenzia delle Dogane e dei Monopoli (ADM), blocked gambling sites.

The page publishes the list as PDF and as TXT (one domain per line) with a
control file, under document-library URLs that change on every update. The plan
reads the page and takes the TXT link.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from registerwatch.registers.base import Bundle, Column, Fetch, Part, Register, Table
from registerwatch.registers.extract import dedupe, is_domain, text_lines

PAGE = "https://www.adm.gov.it/portale/siti-web-inibiti-giochi"
_TXT = re.compile(r'href="([^"]*elenco_siti_inibiti_giochi\.txt[^"]*)"', re.I)


def plan(fetch: Fetch) -> list[Part]:
    m = _TXT.search(fetch(PAGE).decode("utf-8", "replace"))
    return [Part("blocklist", urljoin(PAGE, m.group(1).replace("&amp;", "&")) if m else None, "txt")]


def _domains(b: Bundle):
    rows = [{"domain": d.lower(), "well_formed": is_domain(d.lower())} for d in text_lines(b["blocklist"])]
    return dedupe(rows, ["domain"])


REGISTER = Register(
    slug="it_adm",
    name="Sites subject to blocking — gambling",
    regulator="Agenzia delle Dogane e dei Monopoli (ADM)",
    country="IT",
    kind="blocklist",
    homepage=PAGE,
    plan=plan,
    count_delta_tolerance=0.05,
    tables=(
        Table("blocked_domains", (Column("domain", required=True), Column("well_formed", "bool")), _domains,
              min_rows=1_000),
    ),
)
