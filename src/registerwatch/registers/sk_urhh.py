"""Slovakia — Úrad pre reguláciu hazardných hier (ÚRHH), individual licences.

The register page offers the list as XML, CSV and XLSX; the plan takes the CSV
link from the page. The CSV has a title line above the header.

www.urhh.sk does not send its intermediate certificate (CA Disig R2I2), which
browsers paper over by fetching it themselves. certs/disig_r2i2.pem completes the
chain; it chains to CA Disig Root R2 in the system trust store, so verification
stays on.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from registerwatch.registers.base import Bundle, Column, Fetch, Part, Register, Table
from registerwatch.registers.extract import csv_rows, split_list

PAGE = "https://www.urhh.sk/licencie-2/registre-zoznamy-a-ciselniky/zoznam-udelenych-individualnych-licencii/"
_CSV = re.compile(r'href="([^"]+\.CSV)"', re.I)
HEADER = ["P.č.", "Názov spoločnosti", "IČO", "Obdobie platnosti (od)", "Obdobie platnosti (do)",
          "Názov hazardnej hry", "Kód hazardnej hry", "Druh hazardnej hry"]


def plan(fetch: Fetch) -> list[Part]:
    m = _CSV.search(fetch(PAGE).decode("utf-8", "replace"))
    return [Part("licences", urljoin(PAGE, m.group(1)) if m else None, "csv")]


def _licences(b: Bundle):
    rows = csv_rows(b["licences"], ["seq", "company", "company_id", "valid_from", "valid_to", "game_name",
                                    "game_codes", "game_types"], expected_header=HEADER, skip_lines=1)
    for r in rows:
        r["game_codes"] = split_list(r["game_codes"], r",")
    return rows


REGISTER = Register(
    slug="sk_urhh",
    name="Granted individual licences",
    regulator="Úrad pre reguláciu hazardných hier (ÚRHH)",
    country="SK",
    kind="licensees",
    homepage=PAGE,
    plan=plan,
    extra_ca="disig_r2i2.pem",
    tables=(
        Table("licences", (Column("seq", "int"), Column("company", required=True), Column("company_id"),
                           Column("valid_from", "date"), Column("valid_to", "date"), Column("game_name"),
                           Column("game_codes", "text[]"), Column("game_types")), _licences, min_rows=10),
    ),
)
