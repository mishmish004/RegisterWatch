"""Kahnawà:ke (Mohawk Territory, Quebec, Canada) — Kahnawà:ke Gaming Commission.

One page with two tables: casino software provider authorisation holders, and
interactive permit holders as (operator, URL) pairs — one row per URL. An
operator with no URL listed appears once with an empty URL; that is the
register's data, not a parse failure.
"""

from __future__ import annotations

from registerwatch.registers.base import Bundle, Column, ParseError, Part, Register, Table
from registerwatch.registers.extract import host, html_doc, html_table

URL = "https://gamingcommission.ca/interactive-gaming/permit-holders/"


def _tables(b: Bundle):
    doc = html_doc(b["permit-holders"])
    tables = doc.xpath("//table")
    if len(tables) < 2:
        raise ParseError("NO_TABLE", f"expected 2 tables, found {len(tables)}")
    return tables


def _operators(b: Bundle):
    rows = []
    for row, _ in html_table(_tables(b)[1], ["operator", "url"], expected_header=["OPERATOR", "URL"]):
        row["host"] = host(row["url"])
        rows.append(row)
    return rows


def _software(b: Bundle):
    return [row for row, _ in html_table(_tables(b)[0], ["name"],
                                         expected_header=["CASINO SOFTWARE PROVIDER AUTHORIZATION HOLDER"])]


REGISTER = Register(
    slug="ca_kgc",
    name="Interactive permit holders",
    regulator="Kahnawà:ke Gaming Commission",
    country="CA",
    kind="licensees",
    homepage=URL,
    parts=(Part("permit-holders", URL, "html"),),
    tables=(
        Table("operators", (Column("operator", required=True), Column("url"), Column("host")), _operators,
              min_rows=20),
        Table("software_providers", (Column("name", required=True),), _software),
    ),
)
