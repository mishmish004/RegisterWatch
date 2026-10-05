"""Ireland — Revenue Commissioners, registers of gaming licences and bookmakers.

Revenue licenses gaming and betting until the Gambling Regulatory Authority
(GRAI) register takes over; both registers are published as CSV at fixed URLs.
They name individuals ("relevant officer", sole-trader bookmakers): public
registers, published for this purpose — treat the copies with the same care.
"""

from __future__ import annotations

from registerwatch.registers.base import Bundle, Column, Part, Register, Table
from registerwatch.registers.extract import csv_rows

BASE = "https://www.revenue.ie/en/corporate/documents/statistics/excise/"


def _gaming(b: Bundle):
    return csv_rows(
        b["gaming-licences"],
        ["licence_ref", "licence_type", "licensee_name", "trading_name", "relevant_officer",
         "premises_address", "principal_office_address"],
        expected_header=["Licence Ref", "Licence Type", "Licensee Name", "Trading Name", "Relevant Officer",
                         "Address of the premises at which the licensee carries on gaming",
                         "Address of the licensee's principal Office or place of business"])


def _bookmakers(b: Bundle):
    return csv_rows(
        b["bookmakers"],
        ["licence_ref", "licensee_name", "trading_name", "relevant_officer", "place_of_business", "county"],
        expected_header=["Licence Ref", "Licensee Name", "Trading Name", "Relevant Officer",
                         "Place of Business", "County"])


REGISTER = Register(
    slug="ie_revenue",
    name="Registers of gaming licences and bookmakers",
    regulator="Revenue Commissioners",
    country="IE",
    kind="licensees",
    homepage="https://www.revenue.ie/en/corporate/information-about-revenue/statistics/excise/licences/gaming-licences.aspx",
    parts=(Part("gaming-licences", BASE + "register-of-gaming-licences.csv", "csv"),
           Part("bookmakers", BASE + "register-of-bookmakers.csv", "csv")),
    tables=(
        Table("gaming_licences", (Column("licence_ref", required=True), Column("licence_type"),
                                  Column("licensee_name", required=True), Column("trading_name"),
                                  Column("relevant_officer"), Column("premises_address"),
                                  Column("principal_office_address")), _gaming, min_rows=10),
        Table("bookmakers", (Column("licence_ref", required=True), Column("licensee_name", required=True),
                             Column("trading_name"), Column("relevant_officer"), Column("place_of_business"),
                             Column("county")), _bookmakers, min_rows=10),
    ),
)
