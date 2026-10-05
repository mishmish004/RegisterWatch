"""Jurisdictions: the registers grouped the way people ask about them.

A jurisdiction is a register's `country` code — ISO 3166-1 alpha-2 (GB, DE) or
a subdivision (US-NJ, CA-ON). One jurisdiction can have several registers:
Switzerland has two regulators (ESBK for casinos, Gespa for lotteries and
betting), each with its own blocklist. Codes are matched case-insensitively and
with `-` or `_`, so `gb`, `GB`, `us-nj` and `US_NJ` all work on the command line
and in URLs.
"""

from __future__ import annotations

from registerwatch.registers import all_registers
from registerwatch.registers.base import Register

NAMES = {
    "AU": "Australia", "BE": "Belgium", "CA": "Canada — Kahnawà:ke", "CA-ON": "Canada — Ontario",
    "CH": "Switzerland", "CZ": "Czechia", "DE": "Germany", "EE": "Estonia", "ES": "Spain",
    "FR": "France", "GB": "Great Britain", "GR": "Greece", "IE": "Ireland", "IM": "Isle of Man",
    "IT": "Italy", "PL": "Poland", "PT": "Portugal", "SE": "Sweden", "SK": "Slovakia",
    "US-NJ": "United States — New Jersey",
}


def normalise(code: str) -> str:
    return code.strip().upper().replace("_", "-")


def by_code() -> dict[str, list[Register]]:
    out: dict[str, list[Register]] = {}
    for r in all_registers():
        out.setdefault(normalise(r.country), []).append(r)
    return dict(sorted(out.items()))


def resolve(code: str) -> list[Register]:
    regs = by_code().get(normalise(code))
    if not regs:
        raise KeyError(f"unknown jurisdiction {code!r}; known: {', '.join(c.lower() for c in by_code())}")
    return regs


def name(code: str) -> str:
    return NAMES.get(normalise(code), normalise(code))


def cli_name(code: str) -> str:
    return normalise(code).lower()


def describe(code: str) -> dict:
    regs = resolve(code)
    return {
        "code": normalise(code),
        "name": name(code),
        "registers": [{"slug": r.slug, "regulator": r.regulator, "name": r.name, "kind": r.kind,
                       "homepage": r.homepage, "schema": r.slug,
                       "tables": {t.name: t.column_names for t in r.tables}} for r in regs],
    }
