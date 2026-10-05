"""Sweden — Spelinspektionen, the licence register.

The register's own page is a client-side app, but the app's "export to Excel"
button calls a plain GET (/api/export/LicenseRegistryExcel) that returns the
whole register as one sheet: every licence, land-based and online, with
addresses, validity and status. That export is the source here.
"""

from __future__ import annotations

from registerwatch.registers.base import Bundle, Column, Part, Register, Table
from registerwatch.registers.extract import host, xlsx_rows

URL = "https://www.spelinspektionen.se/api/export/LicenseRegistryExcel"

HEADER = ["Adress 1", "Adress 2", "Aktör", "Butiks-ID", "Inlösar-ID", "Land", "Licens fr.o.m",
          "Licens t.o.m", "Licenstyp", "Notering", "Noteringstyp", "Noteringswebbadress",
          "Platsadress 1", "Platsadress 2", "Platsland", "Platsnamn", "Platspostnr", "Platsstad",
          "Postnr", "Stad", "Status", "Webbadress"]
COLUMNS = ["address_1", "address_2", "operator", "shop_id", "redeemer_id", "country", "valid_from",
           "valid_to", "licence_type", "note", "note_type", "note_url", "site_address_1",
           "site_address_2", "site_country", "site_name", "site_postcode", "site_city",
           "postcode", "city", "status", "website"]

LICENCE_TYPES = frozenset({
    "Statligt värdeautomater", "Landbaserat kasinospel", "Allmännytta bingo", "Spelprogramvara",
    "Kommersiellt online", "Kommersiellt vadhållning", "Allmännytta lotteri", "Fartyg internationell trafik",
    "Landbaserat varuspel", "Allmännytta lokalt poolspel", "Landbaserat kortspel", "Statligt lotteri",
})


def _licences(b: Bundle):
    rows = []
    for row, _ in xlsx_rows(b["register"], COLUMNS, expected_header=HEADER):
        row["host"] = host(row["website"])
        rows.append(row)
    return rows


REGISTER = Register(
    slug="se_si",
    name="Licence register",
    regulator="Spelinspektionen",
    country="SE",
    kind="licensees",
    homepage="https://www.spelinspektionen.se/licens-o-tillstand/licensregister/",
    parts=(Part("register", URL, "xlsx"),),
    count_delta_tolerance=0.05,
    tables=(
        Table("licences", tuple(Column(c, "date" if c in ("valid_from", "valid_to") else "text",
                                       required=c in ("operator", "licence_type"))
                                for c in COLUMNS) + (Column("host"),),
              _licences, min_rows=100,
              known_values={"licence_type": LICENCE_TYPES, "status": frozenset({"Aktiv", "Inaktiv"})},
              description="Every licence, land-based and online; one row per licence/site"),
    ),
)
