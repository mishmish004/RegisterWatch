"""Canonical bytes, then hash. One definition, used by every stage.

Two different questions get asked of a snapshot and they need two different
hashes:

  raw_hash        did a single byte change?          -> hash the bytes as fetched
  canonical hash  did anything *meaningful* change?  -> hash the bytes after the
                  publisher's cosmetic churn is normalised away

The second one exists because registers re-export nightly. A BOM appears, the
export host flips CRLF, the row order follows whatever the upstream query
planner felt like, and every field gains quotes it did not have yesterday. All
four produce a new raw_hash and zero real changes, and a differ that trusts
raw_hash alone would page a customer about them.

What canonicalisation is allowed to do is therefore narrow and stated:

  - strip a UTF-8 BOM
  - normalise CRLF / bare CR to LF
  - re-emit every field with one quoting rule (QUOTE_MINIMAL)
  - strip leading/trailing whitespace per field, including the space after a
    delimiter that would otherwise turn a quoted field into a literal one
  - NFC-normalise text, so "é" and "e+combining-accent" stop alternating
  - drop wholly empty rows
  - sort the data rows; the header keeps its place

What it must never do: touch the inside of a field. "A  B" -> "A B" would be a
lossy edit that silently swallows a real correction upstream.

CANON_VERSION is part of the recorded evidence. Change the rules above and you
change it, so a hash computed under the old rules is never compared against one
computed under the new.
"""

from __future__ import annotations

import csv
import hashlib
import io
import unicodedata
from typing import Iterable

CANON_VERSION = 1

# The register's own exports run to ~4500 rows, but a field can legally hold a
# whole address block; csv's default limit is generous enough and a raised limit
# would only hide a malformed file.


class CanonError(ValueError):
    """The bytes could not be canonicalised. The raw bytes are still evidence."""


def canonicalise_csv(raw: bytes) -> bytes:
    """Return the canonical UTF-8 form of a CSV document.

    Raises CanonError if the bytes are not decodable or not parseable as CSV.
    Callers store the raw bytes regardless: a file we cannot normalise is a
    fact about the register, not a reason to lose the download.
    """
    return canonical_bytes(*read_csv(raw))


def read_csv(raw: bytes) -> tuple[list[str], list[list[str]]]:
    """Decode, parse and clean a CSV document, without reordering it.

    Split out of canonicalise_csv so validation can look at the same rows the
    canonical hash is computed over, instead of parsing the bytes a second way.
    """
    try:
        text = raw.decode("utf-8-sig")  # utf-8-sig strips the BOM if present
    except UnicodeDecodeError as exc:
        raise CanonError(f"not utf-8: {exc}") from exc

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    try:
        # skipinitialspace: `"102", "Active"` is the same record as
        # `"102","Active"`. Without it the second field parses as the literal
        # characters ` "Active"` and the quotes end up inside the value.
        rows = list(csv.reader(io.StringIO(text, newline=""), skipinitialspace=True))
    except csv.Error as exc:
        raise CanonError(f"not parseable as csv: {exc}") from exc

    cleaned = [[_field(f) for f in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        raise CanonError("no rows")
    return cleaned[0], cleaned[1:]


def canonical_bytes(header: list[str], data: list[list[str]]) -> bytes:
    """Re-emit cleaned rows in canonical form: header first, data sorted."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerows([header, *sorted(data)])
    return out.getvalue().encode("utf-8")


def _field(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def combine_hashes(digests: Iterable[bytes]) -> bytes:
    """Fold per-part digests into one, order-sensitively.

    The separator matters: without it, ("ab", "c") and ("a", "bc") would fold to
    the same value, and a part boundary moving is exactly the kind of change
    this is supposed to catch. Callers must pass the digests in a stable order —
    ordinal order for pages, declared-part order for a multi-file download —
    or the fold reports change on every run.
    """
    return hashlib.sha256(b"|".join(digests)).digest()
