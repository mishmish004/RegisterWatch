"""Isle of Man — Gambling Supervision Commission, online gambling licence holders.

Two views of the same register, both taken:

  register page  one key/value table per licensee: status, validity, licence
                 type and — only here — the authorised website domains
  xlsx           "<YY-MM-DD>-current-ogra-licence-holders.xlsx", linked from
                 the page under a dated name; the plan takes the newest

Dates on the page are written two ways ("24/07/2019", "08 May 2025"); both parse.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from registerwatch.registers.base import Bundle, Column, Fetch, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, html_doc, split_list, text, xlsx_rows

URL = "https://www.isleofmangsc.com/gambling/supervision/online-gambling-licensee-register/"
_XLSX = re.compile(r'href="([^"]*/(\d{2})-(\d{2})-(\d{2})-current-ogra-licence-holders\.xlsx)"', re.I)


def plan(fetch: Fetch) -> list[Part]:
    page = fetch(URL).decode("utf-8", "replace")
    found = sorted(_XLSX.findall(page), key=lambda m: (m[1], m[2], m[3]))
    xlsx = urljoin(URL, found[-1][0]) if found else None
    return [Part("register-page", URL, "html"), Part("licence-holders", xlsx, "xlsx")]


def _entries(b: Bundle):
    doc = html_doc(b["register-page"])
    out = []
    for tb in doc.xpath("//table"):
        kv = {}
        for tr in tb.xpath(".//tr"):
            cells = tr.xpath("./td|./th")
            if len(cells) >= 2:
                kv[text(cells[0]) or ""] = cells[1]
        if "Company Name" in kv:
            out.append(kv)
    if not out:
        raise ParseError("NO_LICENSEE_TABLES")
    return out


def _licensees(b: Bundle):
    return [{"company": text(kv["Company Name"]), "status": text(kv.get("Licence Status")),
             "valid_from": text(kv.get("Licence valid From")), "valid_to": text(kv.get("Licence Valid to")),
             "licence_type": text(kv.get("OGRA Licence type"))} for kv in _entries(b)]


def _domains(b: Bundle):
    rows = []
    for kv in _entries(b):
        # Not `a or b`: an lxml element with no children is falsy.
        cell = kv.get("Website Domains")
        if cell is None:
            cell = kv.get("Website Domain")
        if cell is None:
            continue
        lines = [clean(t) for t in cell.xpath(".//text()")]
        for d in (x for line in lines if line for x in split_list(line, r"[,;\s]+") if x.strip("-–— ")):
            rows.append({"company": text(kv["Company Name"]), "domain": d, "host": host(d)})
    return rows


def _xlsx(b: Bundle):
    return [row for row, _ in xlsx_rows(b["licence-holders"],
                                        ["name", "firm_status", "initial_licence_date", "licence_type"],
                                        expected_header=["Name", "Firm Status", "Initial Licence Date",
                                                         "Licence Type"])]


REGISTER = Register(
    slug="im_gsc",
    name="Online gambling licence holders",
    regulator="Gambling Supervision Commission",
    country="IM",
    kind="licensees",
    homepage=URL,
    plan=plan,
    tables=(
        Table("licensees", (Column("company", required=True), Column("status", required=True),
                            Column("valid_from", "date"), Column("valid_to"), Column("licence_type")),
              _licensees, min_rows=10, description="valid_to is text: the register writes 'Current'"),
        Table("domains", (Column("company", required=True), Column("domain", required=True), Column("host")),
              _domains),
        Table("licence_holders", (Column("name", required=True), Column("firm_status"),
                                  Column("initial_licence_date", "date"), Column("licence_type")),
              _xlsx, min_rows=10, description="The dated XLSX the page links"),
    ),
)
