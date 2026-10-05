"""Australia — ACMA, register of licensed interactive gambling providers.

The "check if a gambling operator is legal" page carries the whole register as
one table: trading name, licence holder, URL and the state or territory
regulator that licensed it.
"""

from __future__ import annotations

from registerwatch.registers.base import Bundle, Column, ParseError, Part, Register, Table
from registerwatch.registers.extract import host, html_doc, html_table

URL = "https://www.acma.gov.au/check-if-gambling-operator-legal"


def _providers(b: Bundle):
    tables = html_doc(b["register"]).xpath("//table")
    if not tables:
        raise ParseError("NO_TABLE")
    rows = []
    for row, _ in html_table(tables[0], ["trading_name", "licence_holder", "url", "licensing_authority"],
                             expected_header=["Trading name", "Licence holder", "URL", "Licensing authority"]):
        row["host"] = host(row["url"])
        rows.append(row)
    return rows


REGISTER = Register(
    slug="au_acma",
    name="Register of licensed interactive wagering service providers",
    regulator="Australian Communications and Media Authority (ACMA)",
    country="AU",
    kind="licensees",
    homepage=URL,
    parts=(Part("register", URL, "html"),),
    tables=(
        Table("providers", (Column("trading_name", required=True), Column("licence_holder", required=True),
                            Column("url"), Column("licensing_authority", required=True), Column("host")),
              _providers, min_rows=50),
    ),
)
