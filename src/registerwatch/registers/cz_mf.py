"""Czech Republic — Ministerstvo financí, list of legal gambling operators (ZHH).

The ministry publishes the list as a dated page under a per-year index, each
page linking one XLSX. The plan reads the index, takes the newest page (highest
id in the newest year), and takes the XLSX it links.

The sheet is a matrix: one row per operator, one column pair (land-based,
internet) per game type. A filled cell reads "●" plus "PM:<date>" (právní moc,
legal force), optionally "Ú:<date>" (účinnost, effect) and the domain(s) the
permit covers. The parser turns the matrix into one row per permit.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from registerwatch.registers.base import Bundle, Column, Fetch, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, to_date, xlsx_sheets

INDEX = "https://mf.gov.cz/cs/kontrola-a-regulace/hazardni-hry/prehledy-a-statistiky/prehledy-legalnich-provozovatelu-whiteli"
_ITEM = re.compile(r'href="(/cs/kontrola-a-regulace/hazardni-hry/prehledy-a-statistiky/'
                   r'prehledy-legalnich-provozovatelu-whiteli/(\d{4})/[^"]*?-(\d+))"')
_XLSX = re.compile(r'href="([^"]+\.xlsx)"', re.I)


def plan(fetch: Fetch) -> list[Part]:
    items = _ITEM.findall(fetch(INDEX).decode("utf-8", "replace"))
    if not items:
        return [Part("operators", None, "xlsx")]
    newest = max(items, key=lambda m: (int(m[1]), int(m[2])))
    page_url = urljoin(INDEX, newest[0])
    m = _XLSX.search(fetch(page_url).decode("utf-8", "replace"))
    return [Part("operators", urljoin(page_url, m.group(1)) if m else None, "xlsx")]


def _matrix(b: Bundle):
    sheet = next(iter(xlsx_sheets(b["operators"]).values()), [])
    hdr = next((i for i, r in enumerate(sheet[:10]) if clean(r[0]) == "Provozovatel"), None)
    if hdr is None or len(sheet) < hdr + 4:
        raise ParseError("HEADER_MISMATCH", "no 'Provozovatel' header row")
    if [clean(c) for c in sheet[hdr][:3]] != ["Provozovatel", "IČ nebo jiný obdobný údaj", "Sídlo"]:
        raise ParseError("HEADER_MISMATCH", "|".join(str(c) for c in sheet[hdr][:3]))
    games, channels = sheet[hdr + 2], sheet[hdr + 3]
    labels, current = [], None
    for i in range(len(channels)):
        current = clean(games[i]) or current
        labels.append((current, clean(channels[i])))
    body = [r for r in sheet[hdr + 4:] if clean(r[0]) and (clean(r[1]) or clean(r[2]))]
    return body, labels


def _operators(b: Bundle):
    body, _ = _matrix(b)
    return [{"operator": clean(r[0]), "company_id": _ico(r[1]), "seat": clean(r[2])} for r in body]


def _ico(v) -> str | None:
    """IČO is eight digits; Excel stores some as numbers and drops the zeros."""
    s = clean(v)
    return s.zfill(8) if s and s.isdigit() and len(s) < 8 else s


def _permits(b: Bundle):
    body, labels = _matrix(b)
    rows = []
    for r in body:
        for i in range(3, len(r)):
            cell = clean(r[i])
            if not cell or "●" not in cell:
                continue
            game, channel = labels[i] if i < len(labels) else (None, None)
            pm = re.search(r"PM:\s*([\d.:]+)", cell)
            u = re.search(r"Ú:\s*([\d.:]+)", cell)
            domains = [x for x in (clean(ln) for ln in cell.splitlines())
                       if x and not x.startswith(("●", "PM:", "Ú:")) and "." in x]
            rows.append({"operator": clean(r[0]), "game_type": game, "channel": channel,
                         "final_on": to_date(pm.group(1).replace(":", ".")) if pm else None,
                         "effective_on": to_date(u.group(1).replace(":", ".")) if u else None,
                         "domains": domains, "hosts": [h for h in (host(d) for d in domains) if h]})
    return rows


REGISTER = Register(
    slug="cz_mf",
    name="List of legal gambling operators (Act 186/2016)",
    regulator="Ministerstvo financí České republiky",
    country="CZ",
    kind="licensees",
    homepage=INDEX,
    plan=plan,
    tables=(
        Table("operators", (Column("operator", required=True), Column("company_id", required=True),
                            Column("seat")), _operators, min_rows=10),
        Table("permits", (Column("operator", required=True), Column("game_type", required=True),
                          Column("channel", required=True), Column("final_on", "date"),
                          Column("effective_on", "date"), Column("domains", "text[]"), Column("hosts", "text[]")),
              _permits, min_rows=10,
              known_values={"channel": frozenset({"Land-Based", "Internet"})},
              description="One row per operator × game type × channel; final_on = právní moc"),
    ),
)
