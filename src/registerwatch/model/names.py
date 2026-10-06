"""Legal-entity names -> keys that compare across registers.

Registers spell one company several ways: "RedPlay Limited" and "Redplay
Limited" (the Isle of Man's page and XLSX), "Codere Apuestas España S.L.U." and
"CODERE APUESTAS ESPANA SLU", "Tipico Co. Ltd." and "Tipico Co Ltd". Two keys:

  name_key   accents, case, punctuation and spacing gone, and the legal form
             spelled one way ("Limited" -> "ltd", "S.L.U." -> "slu"), but kept.
             Equal keys are the same name; parties are clustered on it.
  name_core  name_key without its trailing legal form. "Betway Limited" and
             "Betway Spain SA" have different keys and related cores: the same
             family, not the same company. Used to search, never to merge.

Nothing here decides that two companies are one. Equal names across two
registers are a strong signal and are presented as such ("matched on name"),
with each register's own party kept underneath.
"""

from __future__ import annotations

import re
import unicodedata

# Whole phrases first: these become one token before single words are mapped.
_PHRASES = (
    (r"public limited company", "plc"),
    (r"limited liability company", "llc"),
    (r"gesellschaft mit beschrankter haftung", "gmbh"),
    (r"aktiengesellschaft", "ag"),
    (r"societe anonyme", "sa"),
    (r"sociedad anonima", "sa"),
    (r"sociedad limitada", "sl"),
    (r"societa per azioni", "spa"),
    (r"societa a responsabilita limitata", "srl"),
    (r"besloten vennootschap", "bv"),
    (r"naamloze vennootschap", "nv"),
    (r"aktsiaselts", "as"),
    (r"osauhing", "ou"),
    (r"spol sro", "sro"),
    (r"sp zoo", "spzoo"),
)
_SPELLINGS = {"limited": "ltd", "incorporated": "inc", "corporation": "corp", "company": "co"}

# Trailing tokens name_core drops. Only ever at the end: "AS Roma" keeps "as".
LEGAL_FORMS = frozenset({
    "ltd", "plc", "llc", "llp", "lp", "inc", "corp", "co", "gmbh", "mbh", "ag", "kg", "kgaa", "ug", "se",
    "sa", "sl", "slu", "sau", "srl", "spa", "sro", "as", "asa", "bv", "nv", "ab", "oy", "oyj", "aps", "kft",
    "zrt", "nyrt", "sarl", "sas", "sasu", "eurl", "lda", "ou", "uab", "sia", "doo", "dd", "ad", "ead",
    "ood", "spzoo", "pte", "pty", "bhd", "cv", "ehf", "hf",
})


def name_key(name: str | None) -> str | None:
    if not name:
        return None
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r"['’ʼ`´]", "", s.replace("&", " and "))  # "Bally's" -> "ballys"
    s = re.sub(r"[\W_]+", " ", s)
    s = " ".join(_join_initials(s.split()))
    for phrase, short in _PHRASES:
        s = re.sub(rf"\b{phrase}\b", short, s)
    tokens = [_SPELLINGS.get(t, t) for t in s.split()]
    return " ".join(tokens) or None


def name_core(key: str | None) -> str | None:
    """name_key minus trailing legal forms ("tipico co ltd" -> "tipico")."""
    if not key:
        return None
    tokens = key.split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_FORMS:
        tokens.pop()
    return " ".join(tokens)


def slug(key: str) -> str:
    """A name_key as a URL path segment: operator ids are these."""
    return key.replace(" ", "-")


def _join_initials(tokens: list[str]) -> list[str]:
    """Runs of single letters are one abbreviation: "s l u" -> "slu",
    "n i k e" -> "nike". Digits are left alone ("3 102 939256 srl")."""
    out: list[str] = []
    run: list[str] = []
    for t in [*tokens, ""]:
        if len(t) == 1 and t.isalpha():
            run.append(t)
            continue
        if run:
            out.append("".join(run))
            run = []
        if t:
            out.append(t)
    return out
