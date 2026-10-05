"""Great Britain — Gambling Commission, public register of businesses.

The download page links four CSVs (businesses, licences, trading names, domain
names) plus a zip and an xlsx of the same data. Discover the links rather than
hardcoding them — a hardcoded 404 and a quiet register look identical — and take
the CSVs, not the zip: the zip is a sixth artifact that has to agree with the
files inside it, and when it does not, you have to work out which is the register.

Licence numbers are not stable identifiers: the last segment is a version
counter that moves without any other change (~480 of ~4,500 rows in one week of
Aug 2026). The row history keys on the whole row, so a renumbering shows as one
row removed and one added in the same snapshot — which the differ must pair on
account + licence number without its last segment + activity.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from registerwatch.registers.base import Bundle, Column, Fetch, Part, Register, Table
from registerwatch.registers.extract import csv_rows, host

DOWNLOAD_URL = "https://www.gamblingcommission.gov.uk/public-register/businesses/download"

# Hash order and "all of them arrived" are both defined by this tuple.
EXPECTED_PARTS = ("businesses", "licences", "trading-names", "domain-names")
_PREFIX = "business-licence-register-"
_CSV_HREF = re.compile(r"""href\s*=\s*["']([^"']+?\.csv(?:\?[^"']*)?)["']""", re.I)

LICENCE_STATUSES = frozenset({
    "Active", "Expired", "Forfeited", "Lapsed", "Pending", "Revoked",
    "Revoked - Non Payment of Fee", "Surrendered", "Suspended",
})


def part_name(url: str) -> str:
    """`.../business-licence-register-licences.csv?v=2` -> `licences`."""
    stem = urlsplit(url).path.rsplit("/", 1)[-1]
    stem = stem[:-4] if stem.lower().endswith(".csv") else stem
    return stem.removeprefix(_PREFIX).lower()


def asset_links(page_url: str, html: bytes) -> dict[str, str]:
    """Every CSV the download page links, part -> absolute URL."""
    found: dict[str, str] = {}
    for href in _CSV_HREF.findall(html.decode("utf-8", "replace")):
        url = urljoin(page_url, href.strip())
        found.setdefault(part_name(url), url)
    return found


def plan(fetch: Fetch) -> list[Part]:
    found = asset_links(DOWNLOAD_URL, fetch(DOWNLOAD_URL))
    parts = [Part(name, found.get(name), "csv") for name in EXPECTED_PARTS]
    # A new file is a change worth having the bytes of; it does not count
    # towards completeness, and no table reads it until someone writes one.
    parts += [Part(n, u, "csv", expected=False) for n, u in sorted(found.items()) if n not in EXPECTED_PARTS]
    return parts


def _businesses(b: Bundle):
    return csv_rows(b["businesses"], ["account_number", "licence_account_name"],
                    expected_header=["Account Number", "Licence Account Name"])


def _licences(b: Bundle):
    return csv_rows(
        b["licences"],
        ["account_number", "licence_number", "status", "type", "activity", "start_date", "end_date"],
        expected_header=["Account Number", "Licence Number", "Status", "Type", "Activity",
                         "Start Date", "End Date"],
    )


def _trading_names(b: Bundle):
    return csv_rows(b["trading-names"], ["account_number", "trading_name", "status"],
                    expected_header=["Account Number", "Trading Name", "Status"])


def _domain_names(b: Bundle):
    rows = csv_rows(b["domain-names"], ["account_number", "domain_name", "status"],
                    expected_header=["Account Number", "Domain Name", "Status"])
    for r in rows:
        r["host"] = host(r["domain_name"])
    return rows


REGISTER = Register(
    slug="gb_ukgc",
    name="Public register of gambling businesses",
    regulator="Gambling Commission",
    country="GB",
    kind="licensees",
    homepage=DOWNLOAD_URL,
    plan=plan,
    count_delta_tolerance=0.05,
    tables=(
        Table("businesses", (Column("account_number", required=True),
                             Column("licence_account_name", required=True)), _businesses,
              description="Licensed businesses (accounts)"),
        Table("licences", (Column("account_number", required=True), Column("licence_number", required=True),
                           Column("status", required=True), Column("type"), Column("activity"),
                           Column("start_date", "date"), Column("end_date", "date")), _licences,
              known_values={"status": LICENCE_STATUSES},
              description="One row per licence and activity; licence_number's last segment is a version"),
        Table("trading_names", (Column("account_number", required=True), Column("trading_name", required=True),
                                Column("status")), _trading_names,
              known_values={"status": frozenset({"Active", "Inactive"})}),
        Table("domain_names", (Column("account_number", required=True), Column("domain_name", required=True),
                               Column("status"), Column("host")), _domain_names,
              known_values={"status": frozenset({"Active", "Inactive", "White Label"})},
              description="domain_name is free text as published; host is the parsed hostname or NULL"),
    ),
)
