"""Poland — Ministerstwo Finansów, register of domains offering gambling unlawfully.

A documented public API (hazard.mf.gov.pl/api/Register, spec v1.3) returns the
whole register as XML: every blocked domain with its entry number and the time
it was added. Removals are not in the feed — a domain missing from it is no
longer blocked — which the row history records as the row's removal.

DataWpisu carries no timezone; it is Warsaw local time, kept as published.
"""

from __future__ import annotations

from registerwatch.registers.base import Bundle, Column, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, to_date, xml_root

URL = "https://hazard.mf.gov.pl/api/Register"
NS = "{http://www.hazard.mf.gov.pl/2017/03/21/}"


def _domains(b: Bundle):
    root = xml_root(b["register"])
    if root.tag != f"{NS}Rejestr":
        raise ParseError("UNEXPECTED_ROOT", str(root.tag))
    rows = []
    for item in root.iter(f"{NS}PozycjaRejestru"):
        added = clean(item.findtext(f"{NS}DataWpisu"))
        rows.append({"entry_no": item.get("Lp"), "domain": clean(item.findtext(f"{NS}AdresDomeny")),
                     "added_at_local": added, "added_on": to_date(added[:10]) if added else None})
    return rows


REGISTER = Register(
    slug="pl_mf",
    name="Register of domains offering gambling in breach of the Gambling Act",
    regulator="Ministerstwo Finansów",
    country="PL",
    kind="blocklist",
    homepage="https://hazard.mf.gov.pl/",
    parts=(Part("register", URL, "xml"),),
    # ~9 MB generated on request; 30s was not enough on 4 Oct 2026.
    timeout_s=180,
    count_delta_tolerance=0.05,
    tables=(
        Table("blocked_domains", (Column("entry_no", "bigint", required=True), Column("domain", required=True),
                                  Column("added_at_local"), Column("added_on", "date")),
              _domains, min_rows=10_000),
    ),
)
