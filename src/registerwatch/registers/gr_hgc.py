"""Greece — Hellenic Gaming Commission (ΕΕΕΠ), licensees.

A single XLSX at a fixed URL with three sheets: SHORT (counts by type), PIVOT
and FULL. FULL — one row per company and licence type — is the register.
"""

from __future__ import annotations

from registerwatch.registers.base import Bundle, Column, Part, Register, Table
from registerwatch.registers.extract import xlsx_rows

URL = "https://hgc.gov.gr/wp-content/uploads/Licensees.xlsx"


def _licensees(b: Bundle):
    return [row for row, _ in xlsx_rows(b["licensees"], ["company", "licence_type"],
                                        expected_header=["Company", "License Type"], sheet="FULL")]


REGISTER = Register(
    slug="gr_hgc",
    name="Licensees",
    regulator="Hellenic Gaming Commission (ΕΕΕΠ)",
    country="GR",
    kind="licensees",
    homepage="https://www.gamingcommission.gov.gr/index.php/forms/licensees",
    parts=(Part("licensees", URL, "xlsx"),),
    tables=(
        Table("licensees", (Column("company", required=True), Column("licence_type", required=True)),
              _licensees, min_rows=10),
    ),
)
