"""Hostnames as the model compares them.

  host_key     the published website text -> lowercase hostname without a
               leading "www.", or None when it is not a hostname. Registers
               write "https://www.Betclic.pt/", "www.betclic.pt" and
               "betclic.pt" for the same site.
  registrable  the part someone registers: "nj.bet365.com" -> "bet365.com",
               "www.bet365.co.uk" -> "bet365.co.uk" (public suffix list,
               private section included, so "foo.herokuapp.com" stays whole)
  label        registrable without its suffix: "bet365". The same label in two
               jurisdictions (bet365.es, bet365.it) is how one brand shows up
               across registers that never name each other. It is a heuristic,
               and presented as one.

The suffix list is the copy bundled with `publicsuffixlist`, so nothing here
touches the network.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from publicsuffixlist import PublicSuffixList

from registerwatch.registers.extract import host


@lru_cache(maxsize=1)
def _psl() -> PublicSuffixList:
    return PublicSuffixList()


def host_key(value: object) -> str | None:
    h = host(value)
    if not h:
        return None
    h = h.removeprefix("www.")
    return h if "." in h or _is_ip(h) else None


def registrable(h: str | None) -> str | None:
    if not h or _is_ip(h):
        return None
    return _psl().privatesuffix(h)


def label(h: str | None) -> str | None:
    reg = registrable(h)
    if not reg:
        return None
    suffix = _psl().publicsuffix(reg)
    return reg[: -len(suffix) - 1] if suffix and reg.endswith("." + suffix) else None


def parents(h: str) -> list[str]:
    """h and every parent of it down to the registrable domain:
    "a.b.example.co.uk" -> ["a.b.example.co.uk", "b.example.co.uk", "example.co.uk"]."""
    reg = registrable(h)
    if not reg or not h.endswith(reg):
        return [h]
    out = [h]
    while out[-1] != reg:
        out.append(out[-1].split(".", 1)[1])
    return out


def is_under(child: str, parent: str) -> bool:
    return child.endswith("." + parent)


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
    except ValueError:
        return False
    return True
