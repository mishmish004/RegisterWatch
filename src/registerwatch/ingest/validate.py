"""Is this the register, or something that arrived in its place?

A 200 only says the server answered. A maintenance page, a header with no rows,
a half-written export and a page whose layout moved all arrive as a 200, and
each would read to a differ as "the register emptied overnight".

Two levels, two kinds of finding:

  check_part   on the bytes: empty, or HTML where data was expected
  check_table  on the parsed rows: too few, required cells blank, a row count
               that moved too far from the last good run, unknown values

  problems     the run is not the register. Bytes are stored, nothing is
               written to the register's tables, the snapshot is incomplete.
  warnings     the run is the register and something deserves a human look.

Every check is structural or relational. Meaning is the parser's business.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from registerwatch.registers.base import Row, Table

# A required column may be blank in a few rows — registers have holes — but not
# in many: then the parser is reading the wrong cell.
MISSING_TOLERANCE = 0.02


def check_part(raw: bytes, kind: str, content_type: str | None) -> list[str]:
    if not raw.strip():
        return ["EMPTY"]
    if kind != "html" and _looks_like_html(raw, content_type):
        return ["HTML_BODY"]
    ct = (content_type or "").split(";")[0].strip().lower()
    if kind == "html" and ct and not ("html" in ct or ct in ("text/plain", "application/xml", "text/xml")):
        # ACMA's edge sometimes answers the register page with its 59-byte
        # uptime-check JSON. Say that, rather than "the table moved".
        return [f"CONTENT_TYPE:{ct}"]
    return []


@dataclass
class TableCheck:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_table(table: Table, rows: list[Row], *, baseline: int | None, tolerance: float,
                accept_count_delta: bool = False) -> TableCheck:
    """`baseline` is the row count of the last version of this table that
    passed. A move beyond `tolerance` is held: a truncated export and a mass
    revocation look the same from here, and only one of them should reach a
    customer without a human deciding which. `accept_count_delta` is that
    decision, and turns the finding into a warning."""
    check = TableCheck()
    n = len(rows)
    if n < table.min_rows:
        check.problems.append(f"NO_ROWS:{n}<{table.min_rows}")
        return check

    for col in table.columns:
        if not col.required or n == 0:
            continue
        missing = sum(1 for r in rows if r.get(col.name) in (None, ""))
        if missing and missing / n > MISSING_TOLERANCE:
            check.problems.append(f"MISSING:{col.name}:{missing}/{n}")
        elif missing:
            check.warnings.append(f"MISSING:{col.name}:{missing}/{n}")

    if baseline and n != baseline and abs(n - baseline) / baseline > tolerance:
        finding = f"COUNT_DELTA:{baseline}->{n}"
        (check.warnings if accept_count_delta else check.problems).append(finding)

    for col, known in table.known_values.items():
        unknown = sorted({str(r.get(col)) for r in rows if r.get(col) not in (None, "")} - set(known))
        if unknown:
            check.warnings.append(f"UNKNOWN_VALUE:{col}:" + "|".join(unknown)[:200])
    return check


def _looks_like_html(raw: bytes, content_type: str | None) -> bool:
    head = raw[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<!doctype", b"<html", b"<head", b"<body")):
        return True
    # A mislabelled CSV served as text/html is still a CSV; markup is the tell.
    return bool(content_type and "html" in content_type.lower() and head.startswith(b"<"))
