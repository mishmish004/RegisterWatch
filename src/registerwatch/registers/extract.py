"""Bytes -> rows, for every format a regulator publishes in.

Each helper either returns rows or raises ParseError with a stable code. None of
them guesses: a header that does not match is HEADER_MISMATCH, not "probably the
same columns in a different order", because the parser that guesses is the one
that quietly puts licence numbers in the status column.

Values are cleaned the way canon.py cleans them — edges stripped, NFC — and never
edited inside. Empty strings become None so "no end date" and "end date: ''"
are the same thing in the database.
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
import zipfile
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from registerwatch import canon
from registerwatch.registers.base import ParseError, Row

# --- values ------------------------------------------------------------------


def clean(value: Any) -> str | None:
    """Text cell -> stripped NFC text, or None for empty. Non-breaking spaces
    count as spaces: registers paste names out of Word."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    s = unicodedata.normalize("NFC", str(value)).replace("\xa0", " ").strip()
    return s or None


_DATE_FORMATS = (
    "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d.%m.%Y",
    "%d.%m.%Y %H:%M:%S", "%d %B %Y", "%d %b %Y", "%d-%m-%Y",
)


def to_date(value: Any) -> date | None:
    """Lenient: anything unparseable is None, and the raw text stays in the blob.

    A register that writes one date as "08 May 2025" and the next as
    "24/07/2019" (the Isle of Man does) should not fail the run over it.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = clean(value)
    if not s:
        return None
    s = re.sub(r"\s+0:00:00$", "", s)  # "15.1.2020 0:00:00"
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", s)  # 15.1.2020 without zero padding
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def host(value: Any) -> str | None:
    """Website cell -> bare lowercase hostname, or None if it is not one.

    `https://www.Betclic.pt/` -> `www.betclic.pt`. Paths are dropped (the
    website column keeps the published text); a value with spaces or no dot is
    not a hostname and returns None rather than a guess.
    """
    s = clean(value)
    if not s:
        return None
    s = s.lower()
    if not _SCHEME.match(s):
        s = "http://" + s
    try:
        h = urlsplit(s).hostname
    except ValueError:
        return None
    if not h or "." not in h or " " in h:
        return None
    return h.rstrip(".")


def split_list(value: Any, seps: str = r"[,;\n]| - |\s{2,}") -> list[str]:
    s = clean(value)
    if not s:
        return []
    return [p for p in (clean(x) for x in re.split(seps, s)) if p]


# --- CSV ---------------------------------------------------------------------


def csv_rows(
    raw: bytes,
    columns: Sequence[str],
    *,
    expected_header: Sequence[str],
    skip_lines: int = 0,
) -> list[Row]:
    """Rows of a CSV whose header must equal `expected_header` exactly.

    `columns` are the output names, positionally matched to the header.
    `skip_lines` drops a title line above the header (Slovakia's export).
    """
    if skip_lines:
        raw = b"\n".join(raw.splitlines()[skip_lines:])
    try:
        header, data = canon.read_csv(raw)
    except canon.CanonError as exc:
        raise ParseError("CSV_UNREADABLE", str(exc)) from exc
    if header != [clean(h) or "" for h in expected_header]:
        raise ParseError("HEADER_MISMATCH", "|".join(header)[:200])
    ragged = sum(1 for r in data if len(r) != len(header))
    if ragged:
        raise ParseError("RAGGED_ROWS", str(ragged))
    return [{c: clean(v) for c, v in zip(columns, r)} for r in data]


# --- XLSX --------------------------------------------------------------------


def xlsx_sheets(raw: bytes) -> dict[str, list[tuple[Any, ...]]]:
    """Every sheet's non-empty rows. openpyxl is imported here so a register
    that never publishes Excel never pays for it."""
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise ParseError("XLSX_UNREADABLE", str(exc)) from exc
    out = {}
    for ws in wb.worksheets:
        rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
        out[ws.title] = [r for r in rows if any(c not in (None, "") for c in r)]
    wb.close()
    return out


def xlsx_rows(
    raw: bytes,
    columns: Sequence[str],
    *,
    expected_header: Sequence[str],
    sheet: str | None = None,
    header_search_rows: int = 10,
) -> list[tuple[Row, tuple[Any, ...]]]:
    """Rows under the first row (within `header_search_rows`) equal to
    `expected_header`. Returns (row dict, raw cells) so callers can keep
    native date cells; cells beyond the header are ignored."""
    sheets = xlsx_sheets(raw)
    if sheet is not None:
        if sheet not in sheets:
            raise ParseError("NO_SHEET", f"{sheet} not in {list(sheets)}")
        candidates = [sheets[sheet]]
    else:
        candidates = list(sheets.values())
    want = [clean(h) for h in expected_header]
    for rows in candidates:
        for i, r in enumerate(rows[:header_search_rows]):
            if [clean(c) for c in r[: len(want)]] == want:
                body = rows[i + 1:]
                return [({c: clean(v) for c, v in zip(columns, cells)}, cells) for cells in body]
    first = [[clean(c) for c in rows[0]] if rows else [] for rows in candidates]
    raise ParseError("HEADER_MISMATCH", json.dumps(first, ensure_ascii=False)[:200])


# --- HTML --------------------------------------------------------------------


def html_doc(raw: bytes):
    from lxml import html as lxml_html

    try:
        return lxml_html.fromstring(raw)
    except Exception as exc:  # lxml raises a zoo of types on garbage
        raise ParseError("HTML_UNREADABLE", str(exc)) from exc


def text(el: Any) -> str | None:
    if el is None:
        return None
    return clean(re.sub(r"\s+", " ", el.text_content()))


def html_table(
    table_el: Any,
    columns: Sequence[str],
    *,
    expected_header: Sequence[str],
) -> list[tuple[Row, Any]]:
    """Rows of one <table> whose first row equals `expected_header`.
    Returns (row dict, <tr> element) so callers can read links out of cells."""
    trs = table_el.xpath(".//tr")
    if not trs:
        raise ParseError("NO_ROWS_IN_TABLE")
    header = [text(c) or "" for c in trs[0].xpath("./th|./td")]
    want = [clean(h) or "" for h in expected_header]
    if header != want:
        raise ParseError("HEADER_MISMATCH", "|".join(header)[:200])
    out = []
    for tr in trs[1:]:
        cells = tr.xpath("./td|./th")
        if not cells:
            continue
        values = [text(c) for c in cells]
        if not any(values):
            continue
        out.append(({c: v for c, v in zip(columns, values)}, tr))
    return out


def one(doc: Any, xpath: str, what: str) -> Any:
    hits = doc.xpath(xpath)
    if not hits:
        raise ParseError("NO_" + what.upper(), xpath[:120])
    return hits[0]


def links(el: Any) -> list[str]:
    return [h for h in (clean(a.get("href")) for a in el.xpath(".//a")) if h]


# --- JSON / XML / text / PDF ------------------------------------------------------


def json_load(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError("JSON_UNREADABLE", str(exc)) from exc


def xml_root(raw: bytes):
    from lxml import etree

    try:
        # resolve_entities off: this is someone else's file.
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
        return etree.fromstring(raw, parser)
    except etree.XMLSyntaxError as exc:
        raise ParseError("XML_UNREADABLE", str(exc)) from exc


def text_lines(raw: bytes, *, comment: str = "#") -> list[str]:
    try:
        s = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        s = raw.decode("latin-1")
    return [ln.strip() for ln in s.splitlines() if ln.strip() and not ln.strip().startswith(comment)]


def pdf_text(raw: bytes) -> str:
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf raises PdfReadError and friends
        raise ParseError("PDF_UNREADABLE", str(exc)) from exc


_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def is_domain(s: str) -> bool:
    return bool(_DOMAIN.match(s))


def dedupe(rows: Iterable[Row], key: Sequence[str]) -> list[Row]:
    """First occurrence wins. For lists (blocklists) where a repeat carries no
    information; registers with meaningful duplicates must not use this."""
    seen, out = set(), []
    for r in rows:
        k = tuple(r.get(c) for c in key)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out
