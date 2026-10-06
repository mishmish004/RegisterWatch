"""Is this domain allowed here? An answer per jurisdiction, never a bare yes/no.

The question customers ask most has more than two answers. A site can be on a
whitelist under a licence that is suspended; blocked in a jurisdiction where its
sister site is licensed; absent from a register that does not list websites at
all. Each jurisdiction therefore gets one of these verdicts, with the reasons,
the caveats, and the register rows behind it:

  conflict              listed as authorised and on a blocklist, both now
  blocked               on a blocklist now
  authorised            listed now, and nothing the register says contradicts it
  listed_not_operating  listed now, but the listing or its licence is inactive,
                        suspended, revoked, expired…
  blocked_parent        a parent domain is on a blocklist (x.example.com when
                        example.com is listed)
  related_listed        a different host under the same domain is listed
                        (nj.bet365.com when bet365.com is asked)
  previously_listed     was listed, and was removed
  previously_blocked    was blocked, and was removed from the blocklist
  not_listed            the jurisdiction's register lists websites; this host
                        is not on it
  not_blocked           only a blocklist is held here, and this host is not on it
  no_domain_data        the register lists licensees but not their websites

confidence says how directly official register data answers "may this host
offer gambling here?": high when a register names the host and says so, medium
when it names a related host or the evidence is historical, low when the answer
rests on absence, none when the data cannot speak to it.

Pure: the caller supplies the model rows that mention the host (and its parents,
children and siblings) and today's date.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from registerwatch import jurisdictions
from registerwatch.model import catalogue, domains
from registerwatch.model.facts import NOT_OPERATING, OPERATING
from registerwatch.registers import REGISTRY
from registerwatch.registers.base import Register

LABELS = {
    "conflict": "Listed and blocked",
    "blocked": "Blocked",
    "authorised": "Authorised listing",
    "listed_not_operating": "Listed, not operating",
    "blocked_parent": "Parent domain blocked",
    "related_listed": "Related host listed",
    "previously_listed": "Previously listed",
    "previously_blocked": "Previously blocked",
    "not_listed": "Not listed",
    "not_blocked": "Not blocked",
    "no_domain_data": "No domain data",
}
CONFIDENCE = {
    "conflict": "high", "blocked": "high", "authorised": "high", "listed_not_operating": "high",
    "blocked_parent": "medium", "related_listed": "low", "previously_listed": "medium",
    "previously_blocked": "medium", "not_listed": "low", "not_blocked": "low", "no_domain_data": "none",
}
BLOCKED = frozenset({"conflict", "blocked", "blocked_parent"})


def relation(target: str, other: str | None) -> str | None:
    """How a listed host relates to the one asked about."""
    if not other:
        return None
    if other == target:
        return "exact"
    if domains.is_under(target, other):
        return "parent"
    if domains.is_under(other, target):
        return "child"
    reg = domains.registrable(target)
    return "sibling" if reg and domains.registrable(other) == reg else None


def assess(target: str, *, listings: Iterable[Mapping[str, Any]], blocks: Iterable[Mapping[str, Any]],
           licences: Mapping[str, Mapping[str, Any]], parties: Mapping[str, Mapping[str, Any]],
           brands: Mapping[str, Mapping[str, Any]], registers: Iterable[Register], today: date) -> dict[str, Any]:
    """`licences` must include every licence of every party that has a listing
    here, not only the linked ones: a listing whose party holds no operating
    licence in that register is not authorised, whatever its own status says."""
    by_jur: dict[str, list[Register]] = {}
    for r in registers:
        by_jur.setdefault(jurisdictions.normalise(r.country), []).append(r)
    party_lics: dict[str, list[Mapping[str, Any]]] = {}
    for lic in licences.values():
        if lic.get("party_id"):
            party_lics.setdefault(lic["party_id"], []).append(lic)
    ctx = _Context(target, licences, parties, brands, party_lics, today)

    matches = [ctx.listing(row) for row in listings if relation(target, row.get("host"))]
    matches += [ctx.block(row) for row in blocks if relation(target, row.get("host"))]
    verdicts = [_verdict(code, regs, [m for m in matches if m["jurisdiction"] == code], ctx)
                for code, regs in sorted(by_jur.items())]
    return {
        "domain": target,
        "registrable": domains.registrable(target),
        "label": domains.label(target),
        "authorised_in": [v["jurisdiction"] for v in verdicts if v["verdict"] == "authorised"],
        "blocked_in": [v["jurisdiction"] for v in verdicts if v["verdict"] in BLOCKED],
        "attention": [v["jurisdiction"] for v in verdicts
                      if v["verdict"] in ("conflict", "listed_not_operating", "related_listed")
                      or (v["verdict"] == "authorised" and v["caveats"])],
        "verdicts": verdicts,
    }


class _Context:
    def __init__(self, target, licences, parties, brands, party_lics, today) -> None:
        self.target = target
        self.licences = licences
        self.parties = parties
        self.brands = brands
        self.party_lics = party_lics
        self.today = today

    def _party(self, pid: str | None) -> dict[str, Any] | None:
        p = self.parties.get(pid) if pid else None
        return {"id": pid, "name": p.get("name"), "operator_id": p.get("cluster_id")} if p else None

    def _licence(self, lid: str | None) -> dict[str, Any] | None:
        lic = self.licences.get(lid) if lid else None
        if not lic:
            return None
        return {k: lic.get(k) for k in ("licence_id", "reference", "type", "products", "status", "status_raw",
                                        "status_since", "valid_from", "valid_to", "regulator", "authority", "area",
                                        "current", "removed_at")}

    def listing(self, row: Mapping[str, Any]) -> dict[str, Any]:
        brand = self.brands.get(row.get("brand_id")) if row.get("brand_id") else None
        return {
            "kind": "listing", "relation": relation(self.target, row["host"]), "current": row["current"],
            "jurisdiction": row["jurisdiction"], "register": row["register"], "host": row["host"],
            "published": row["published"], "status": row["status"], "status_raw": row["status_raw"],
            "party": self._party(row.get("party_id")), "brand": brand.get("name") if brand else None,
            "licence": self._licence(row.get("licence_id")), "products": row.get("products") or [],
            "since": row.get("since"), "first_seen_at": row.get("first_seen_at"),
            "removed_at": row.get("removed_at"), "evidence": row.get("evidence") or [],
            "_row": row,
        }

    def block(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "kind": "block", "relation": relation(self.target, row["host"]), "current": row["current"],
            "jurisdiction": row["jurisdiction"], "register": row["register"], "host": row["host"],
            "published": row["published"], "listed_on": row.get("listed_on"),
            "first_seen_at": row.get("first_seen_at"), "removed_at": row.get("removed_at"),
            "evidence": row.get("evidence") or [], "_row": row,
        }

    def operating(self, m: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
        """(operating?, reasons it is not, caveats) for a current listing."""
        row = m["_row"]
        reasons: list[str] = []
        caveats: list[str] = []
        if row["status"] not in OPERATING:
            reasons.append(f"the listing's status is {row['status_raw'] or row['status']}")
        lic = self.licences.get(row.get("licence_id")) if row.get("licence_id") else None
        if lic:
            if not lic.get("current"):
                reasons.append("the licence it is listed under is no longer on the register")
            elif lic["status"] in NOT_OPERATING or lic["status"] == "pending":
                since = f" since {lic['status_since']}" if lic.get("status_since") else ""
                reasons.append(f"its licence is {lic['status_raw'] or lic['status']}{since}")
            elif lic.get("valid_to") and lic["valid_to"] < self.today:
                caveats.append(f"Its licence's end date ({lic['valid_to']}) has passed, but the register still "
                               "lists it.")
        elif row.get("party_id"):
            held = [x for x in self.party_lics.get(row["party_id"], []) if x["register"] == row["register"]]
            if held and not any(x.get("current") and x["status"] in OPERATING for x in held):
                reasons.append("none of the party's licences on this register is active")
        if row["status"] == "white_label":
            caveats.append("Listed as a white label: the licence is the listed account's, not the brand's.")
        if row["register"] == "us_nj_dge":
            caveats.append("New Jersey lists sites under the Atlantic City licensee holding the permit; the "
                           "brand may be run by a partner.")
        return not reasons, reasons, caveats


def _who(m: dict[str, Any]) -> str:
    """" for Acme Ltd under licence 123, issued by Liquor & Gaming NSW"."""
    out = f" for {m['party']['name']}" if m.get("party") else ""
    lic = m.get("licence") or {}
    name = lic.get("reference") or lic.get("type")
    by = lic.get("authority") if lic.get("authority") != lic.get("regulator") else None
    if name:
        out += f" under licence {name}" + (f", issued by {by}" if by else "")
    elif by:
        out += f" under a licence issued by {by}"
    return out


def _verdict(code: str, regs: list[Register], ms: list[dict[str, Any]], ctx: _Context) -> dict[str, Any]:
    covers = set().union(*(catalogue.coverage(r.slug).covers for r in regs))
    regulators = ", ".join(r.regulator for r in regs)
    t = ctx.target

    def pick(kind: str, rel: str | tuple[str, ...], current: bool) -> list[dict[str, Any]]:
        rels = (rel,) if isinstance(rel, str) else rel
        return [m for m in ms if m["kind"] == kind and m["relation"] in rels and m["current"] == current]

    listed, blocked = pick("listing", "exact", True), pick("block", "exact", True)
    operating, reasons, caveats = [], [], []
    for m in listed:
        ok, why, cav = ctx.operating(m)
        if ok:
            operating.append(m)
        reasons += why
        caveats += cav
    caveats = list(dict.fromkeys(caveats))

    if listed and blocked:
        verdict = "conflict"
        text = f"{t} is listed{_who(listed[0])} and is {_on_blocklist(blocked)} at the same time."
    elif blocked:
        verdict = "blocked"
        since = next((m["listed_on"] for m in blocked if m.get("listed_on")), None)
        text = f"{t} is {_on_blocklist(blocked)}" + (f", listed {since}." if since else ".")
    elif operating:
        verdict = "authorised"
        text = f"{t} is listed{_who(operating[0])}."
    elif listed:
        verdict = "listed_not_operating"
        text = f"{t} is listed{_who(listed[0])}, but {'; '.join(dict.fromkeys(reasons))}."
    elif parent := pick("block", "parent", True):
        verdict = "blocked_parent"
        text = f"{parent[0]['host']}, a parent of {t}, is {_on_blocklist(parent)}."
        caveats.append("A blocklist entry usually reaches its subdomains, but the order names the parent.")
    elif related := pick("listing", ("parent", "child", "sibling"), True):
        verdict = "related_listed"
        hosts = sorted({m["host"] for m in related})
        text = f"{t} is not listed, but {', '.join(hosts[:5])}{' …' if len(hosts) > 5 else ''} is."
        caveats.append("A listing covers the host it names; check whether this host is run under the same "
                       "licence.")
    elif gone := pick("listing", "exact", False):
        verdict = "previously_listed"
        when = max((m["removed_at"] for m in gone if m.get("removed_at")), default=None)
        text = f"{t} was listed{_who(gone[0])} and was removed" + (f" on {when:%Y-%m-%d}." if when else ".")
    elif gone := pick("block", "exact", False):
        verdict = "previously_blocked"
        when = max((m["removed_at"] for m in gone if m.get("removed_at")), default=None)
        text = f"{t} was {_on_blocklist(gone)} and was removed" + (f" on {when:%Y-%m-%d}." if when else ".")
    elif "domain" in covers:
        verdict = "not_listed"
        text = f"{t} is not among the websites {regulators} lists."
        caveats.append("Not being listed is not proof a site is unlawful here: registers lag approvals and "
                       "some list only an operator's main domains.")
    elif "block" in covers and covers <= {"block"}:
        verdict = "not_blocked"
        text = f"{t} is not {_on_blocklist([{'register': r.slug} for r in regs])}."
        caveats.append("Only a blocklist is held for this jurisdiction: not being blocked does not make a "
                       "site authorised.")
        caveats += [f"{r.regulator}: {catalogue.coverage(r.slug).scope}" for r in regs if len(regs) > 1]
    else:
        verdict = "no_domain_data"
        text = f"{regulators} lists licensees but not their websites, so the register cannot confirm or deny {t}."

    return {
        "jurisdiction": code, "name": jurisdictions.name(code), "verdict": verdict, "label": LABELS[verdict],
        "confidence": "medium" if verdict == "authorised" and caveats else CONFIDENCE[verdict],
        "explanation": text, "caveats": caveats, "registers": [r.slug for r in regs],
        "matches": [{k: v for k, v in m.items() if k != "_row"} for m in ms],
    }


def _on_blocklist(ms: list[dict[str, Any]]) -> str:
    """"on ADM's blocklist", or "on the blocklists of ESBK and Gespa"."""
    regs = list(dict.fromkeys(REGISTRY[m["register"]].regulator for m in ms))
    return f"on {regs[0]}'s blocklist" if len(regs) == 1 else f"on the blocklists of {' and '.join(regs)}"
