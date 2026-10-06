"""One projection per register: its rows -> facts. Pure, like the parsers.

A projection gets every row of every table of its register — current and
removed, each with its RowRef — and yields facts (facts.py). It decides three
things the register cannot say for itself:

  identity  the key that makes two rows versions of one thing. UKGC: account +
            licence number without its version segment + activity. Sweden:
            operator + licence type + site, so the three websites of one online
            licence are one licence. A blocklist: the hostname.
  links     which party a licence belongs to, which licence a domain is listed
            under — always within the register, by key
  meaning   status, products, channel, in the shared vocabularies

What it must not do is invent: a register that lists operators without saying
what they are licensed for gets party and domain facts, not licence facts with a
guessed type. Named individuals that some registers carry beside a licence
(Ireland's "relevant officer") are not projected at all.

Adding a register: write its projection here and add it to PROJECTIONS; a test
fails until every register has one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

from registerwatch.model.domains import host_key
from registerwatch.model.facts import Fact, RowRef, block, brand, channel, domain, licence, party, products
from registerwatch.model.names import name_key
from registerwatch.registers.extract import to_date

Row = Mapping[str, Any]
Rows = Mapping[str, Sequence[tuple[RowRef, Row]]]
Projection = Callable[[Rows], Iterable[Fact]]


def _pk(name: str | None) -> str | None:
    """Party key for registers that name a party but give no identifier."""
    k = name_key(name)
    return f"name:{k}" if k else None


def _named(t: Rows, table: str) -> Iterator[tuple[RowRef, Row]]:
    yield from t.get(table, ())


def _blocks(t: Rows, table: str, date_col: str | None = None) -> Iterator[Fact]:
    # Keyed on the entry as published: Poland lists "www.x.com" and "x.com" as
    # two entries, and either can be removed without the other.
    for ref, r in _named(t, table):
        yield block(ref, r["domain"].casefold(), host=host_key(r["domain"]), published=r["domain"],
                    listed_on=r.get(date_col) if date_col else None)


def _domain(ref: RowRef, owner: str | None, published: str | None, **values: Any) -> Fact | None:
    """A listing: one published website entry, keyed on what it is listed under
    (licence, brand or party) and the entry itself — "berriez.com/nz" and
    "berriez.com/en" are two listings of one host. None for an empty cell."""
    if not published:
        return None
    return domain(ref, f"{owner or ''}|{published.casefold()}", host=host_key(published), published=published,
                  **values)


def _unique(keys: set[str]) -> str | None:
    return next(iter(keys)) if len(keys) == 1 else None


# --- registers, alphabetically -------------------------------------------------------------

def au_acma(t: Rows) -> Iterator[Fact]:
    # Every provider here is licensed by a state or territory regulator to
    # offer wagering; the register says which one.
    for ref, r in _named(t, "providers"):
        pk = _pk(r["licence_holder"])
        lk = f"{pk}|{name_key(r['licensing_authority'])}"
        bk = f"{pk}|{name_key(r['trading_name'])}"
        yield party(ref, pk, r["licence_holder"])
        yield licence(ref, lk, party=pk, products=("betting",), channel="online", authority=r["licensing_authority"])
        yield brand(ref, bk, party=pk, name=r["trading_name"], products=("betting",))
        # "No current URL" is the register's own wording for a brand that is not trading.
        if host_key(r["url"]) and (d := _domain(ref, lk, r["url"], party=pk, brand=bk, licence=lk,
                                                products=("betting",))):
            yield d


_BE_PRODUCTS = {"A": ("casino",), "APLUS": ("casino",), "B": ("gaming_machines",), "BPLUS": ("casino",),
                "C": ("gaming_machines",), "E": ("b2b",)}


def _be_licence(ref: RowRef, r: Row, pk: str | None, site: str | None) -> tuple[str, Fact]:
    cls = r["licence_class"]
    lk = f"{cls}|{r['dossier_id']}"
    online = cls.endswith("PLUS")
    prods = _BE_PRODUCTS.get(cls, ("betting",) if cls.startswith("F") else ())
    return lk, licence(ref, lk, party=pk, reference=r["dossier_id"],
                       type=f"Class {cls.removesuffix('PLUS')}{'+' if online else ''}", products=prods,
                       channel="online" if online else "land", site=site, valid_from=r["decision_date"],
                       valid_to=r["expiration_date"])


def be_gc(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "online_licences"):
        pk = _pk(r["operator"])
        lk, lic = _be_licence(ref, r, pk, r["establishment"])
        yield party(ref, pk, r["operator"])
        yield lic
        if d := _domain(ref, lk, r["website"], party=pk, licence=lk, products=lic.get("products")):
            yield d
    for ref, r in _named(t, "establishments"):
        pk = _pk(r["owner"])
        yield party(ref, pk, r["owner"])
        yield _be_licence(ref, r, pk, r["establishment"])[1]


def ca_kgc(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "operators"):
        pk = _pk(r["operator"])
        lk = f"{pk}|interactive"
        yield party(ref, pk, r["operator"])
        yield licence(ref, lk, party=pk, type="Interactive permit", channel="online")
        if d := _domain(ref, lk, r["url"], party=pk, licence=lk):
            yield d
    for ref, r in _named(t, "software_providers"):
        pk = _pk(r["name"])
        yield party(ref, pk, r["name"])
        yield licence(ref, f"{pk}|software", party=pk, type="Casino software provider authorization",
                      products=("b2b",), channel="online")


def ca_on_igo(t: Rows) -> Iterator[Fact]:
    # Brands live in Ontario's regulated market; the page does not name the
    # company behind each one.
    for ref, r in _named(t, "brands"):
        bk = f"name:{name_key(r['brand'])}"
        prods = products(*(r["offerings"] or ()))
        yield brand(ref, bk, name=r["brand"], products=prods)
        if d := _domain(ref, bk, r["website"], brand=bk, products=prods):
            yield d


def ch_esbk(t: Rows) -> Iterator[Fact]:
    yield from _blocks(t, "blocked_domains", "listed_on")


def ch_gespa(t: Rows) -> Iterator[Fact]:
    yield from _blocks(t, "blocked_domains")


def _cz_products(game_type: str | None, chan: str | None) -> tuple[str, ...]:
    # "Technické hry" are slot-type games: online that is casino, on land it is
    # a gaming hall's machines.
    if game_type == "Technické hry":
        return ("casino",) if chan == "online" else ("gaming_machines",)
    return products(game_type)


def cz_mf(t: Rows) -> Iterator[Fact]:
    by_name: dict[str | None, str] = {}
    for ref, r in _named(t, "operators"):
        pk = f"ico:{r['company_id']}" if r["company_id"] else _pk(r["operator"])
        by_name[name_key(r["operator"])] = pk
        yield party(ref, pk, r["operator"], ico=r["company_id"])
    for ref, r in _named(t, "permits"):
        pk = by_name.get(name_key(r["operator"])) or _pk(r["operator"])
        chan = channel(r["channel"])
        prods = _cz_products(r["game_type"], chan)
        lk = f"{pk}|{r['game_type']}|{r['channel']}"
        yield licence(ref, lk, party=pk, type=r["game_type"], products=prods, channel=chan,
                      valid_from=r["effective_on"] or r["final_on"])
        for d in r["domains"] or ():
            if f := _domain(ref, lk, d, party=pk, licence=lk, products=prods):
                yield f


def de_ggl(t: Rows) -> Iterator[Fact]:
    by_type: dict[tuple[str | None, str | None], set[str]] = {}
    for ref, r in _named(t, "permits"):
        pk = _pk(r["operator"])
        lk = f"{pk}|{r['gambling_type']}|{r['authority']}|{r['sales_area']}"
        by_type.setdefault((pk, r["gambling_type"]), set()).add(lk)
        yield party(ref, pk, r["operator"])
        yield licence(ref, lk, party=pk, type=r["gambling_type"], products=products(r["gambling_type"]),
                      channel=channel(r["distribution_channel"]), area=r["sales_area"], authority=r["authority"])
    for ref, r in _named(t, "websites"):
        pk = _pk(r["operator"])
        # The website block names the gambling type, not the permit; it links
        # when the operator holds exactly one permit of that type.
        lk = _unique(by_type.get((pk, r["gambling_type"]), set()))
        if d := _domain(ref, lk or f"{pk}|{r['gambling_type']}", r["website"], party=pk, licence=lk,
                        products=products(r["gambling_type"]), since=r["first_permit_date"]):
            yield d


_EE_SUBTYPES = {"online": ("Online gambling", ()), "casino": ("Casino", ("casino",)),
                "toto": ("Totalisator", ("betting",)), "ship": ("Gambling on ships", ()),
                "games-skill": ("Games of skill", ()), "lotteries": ("Lotteries", ("lottery",))}


def ee_emta(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "operators"):
        pk = _pk(r["operator"])
        label, prods = _EE_SUBTYPES.get(r["subtype"], (r["subtype"], ()))
        yield party(ref, pk, r["operator"])
        yield licence(ref, f"{pk}|{r['subtype']}", party=pk, type=label, products=prods,
                      channel="online" if r["subtype"] == "online" else None, reference=r["register_url"])
        if r["brand"]:
            # One brand appears under several subtypes; its products are the licences'.
            yield brand(ref, f"{pk}|{name_key(r['brand'])}", party=pk, name=r["brand"])
    for ref, r in _named(t, "websites"):
        pk = _pk(r["operator"])
        lk = f"{pk}|{r['subtype']}" if r["subtype"] else None
        if d := _domain(ref, lk or pk, r["website"], party=pk, licence=lk,
                        products=_EE_SUBTYPES.get(r["subtype"], (None, ()))[1]):
            yield d


def es_dgoj(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "operators"):
        yield party(ref, _pk(r["operator"]), r["operator"], dgoj_url=r["detail_url"])
    for ref, r in _named(t, "websites"):
        pk = _pk(r["operator"])
        if d := _domain(ref, pk, r["website"], party=pk):
            yield d


def fr_anj(t: Rows) -> Iterator[Fact]:
    # ANJ approves an operator per category; the page does not say which of
    # its sites carries which category, so the sites carry none.
    for ref, r in _named(t, "operators"):
        pk = _pk(r["operator"])
        yield party(ref, pk, r["operator"])
        for cat in r["categories"] or ():
            yield licence(ref, f"{pk}|{name_key(cat)}", party=pk, type=cat, products=products(cat),
                          channel="online")
    for ref, r in _named(t, "websites"):
        pk = _pk(r["operator"])
        if d := _domain(ref, pk, r["website"], party=pk):
            yield d


def _ukgc_base(number: str | None) -> str | None:
    """"000102-N-317976-010" -> "000102-N-317976": the last segment is a
    version counter that moves without any other change."""
    if number and number.count("-") == 3:
        return number.rsplit("-", 1)[0]
    return number


def gb_ukgc(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "businesses"):
        acct = r["account_number"]
        yield party(ref, f"acct:{acct}", r["licence_account_name"], ukgc_account=acct)
    for ref, r in _named(t, "licences"):
        pk = f"acct:{r['account_number']}"
        yield licence(ref, f"{pk}|{_ukgc_base(r['licence_number'])}|{r['activity']}", party=pk,
                      reference=r["licence_number"], type=r["activity"], products=products(r["activity"]),
                      channel=channel(r["type"]), status_raw=r["status"], valid_from=r["start_date"],
                      valid_to=r["end_date"])
    for ref, r in _named(t, "trading_names"):
        pk = f"acct:{r['account_number']}"
        yield brand(ref, f"{pk}|{name_key(r['trading_name'])}", party=pk, name=r["trading_name"],
                    status_raw=r["status"])
    for ref, r in _named(t, "domain_names"):
        pk = f"acct:{r['account_number']}"
        if d := _domain(ref, pk, r["domain_name"], party=pk, status_raw=r["status"]):
            yield d


_GR_PRODUCTS = {"Betting (Type 1)": ("betting",), "Other Online Games (Type 2)": ("casino", "poker"),
                "Land Based Casino": ("casino",)}


def gr_hgc(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "licensees"):
        pk = _pk(r["company"])
        lt = r["licence_type"]
        yield party(ref, pk, r["company"])
        yield licence(ref, f"{pk}|{lt}", party=pk, type=lt, products=_GR_PRODUCTS.get(lt, ()),
                      channel="land" if lt == "Land Based Casino" else ("online" if "Type" in (lt or "") else None))


def _trading(name: str | None) -> str | None:
    return None if not name or name.strip().upper() in ("NA", "N/A", "-") else name


def ie_revenue(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "gaming_licences"):
        pk = _pk(r["licensee_name"])
        yield party(ref, pk, r["licensee_name"])
        yield licence(ref, f"ref:{r['licence_ref']}", party=pk, reference=r["licence_ref"], type=r["licence_type"],
                      channel="land", site=r["premises_address"])
        if tn := _trading(r["trading_name"]):
            yield brand(ref, f"{pk}|{name_key(tn)}", party=pk, name=tn)
    for ref, r in _named(t, "bookmakers"):
        pk = _pk(r["licensee_name"])
        yield party(ref, pk, r["licensee_name"])
        yield licence(ref, f"ref:{r['licence_ref']}", party=pk, reference=r["licence_ref"],
                      type="Bookmaker's licence", products=("betting",), area=r["county"],
                      site=r["place_of_business"])
        if tn := _trading(r["trading_name"]):
            yield brand(ref, f"{pk}|{name_key(tn)}", party=pk, name=tn, products=("betting",))


def im_gsc(t: Rows) -> Iterator[Fact]:
    by_party: dict[str | None, set[str]] = {}
    for ref, r in _named(t, "licensees"):
        pk = _pk(r["company"])
        lk = f"{pk}|{r['licence_type']}"
        by_party.setdefault(pk, set()).add(lk)
        yield party(ref, pk, r["company"])
        yield licence(ref, lk, party=pk, type=r["licence_type"], products=products(r["licence_type"]),
                      channel="online", status_raw=r["status"], valid_from=r["valid_from"],
                      valid_to=to_date(r["valid_to"]), notes=r["valid_to"] if not to_date(r["valid_to"]) else None)
    for ref, r in _named(t, "licence_holders"):
        yield party(ref, _pk(r["name"]), r["name"])
    for ref, r in _named(t, "domains"):
        pk = _pk(r["company"])
        lk = _unique(by_party.get(pk, set()))
        if d := _domain(ref, lk or pk, r["domain"], party=pk, licence=lk):
            yield d


def it_adm(t: Rows) -> Iterator[Fact]:
    yield from _blocks(t, "blocked_domains")


def pl_mf(t: Rows) -> Iterator[Fact]:
    yield from _blocks(t, "blocked_domains", "added_on")


def pt_srij(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "brands"):
        pk = _pk(r["operator"])
        name = r["brand"] or r["title"]
        bk = f"{pk}|{name_key(name)}" if name else None
        yield party(ref, pk, r["operator"])
        if bk:
            yield brand(ref, bk, party=pk, name=name)
        if d := _domain(ref, bk or pk, r["website"], party=pk, brand=bk):
            yield d


# "Kommersiellt online" is the commercial online gaming licence (casino-type
# games); betting is licensed separately as "Kommersiellt vadhållning".
_SE_PRODUCTS = {"Kommersiellt online": ("casino",)}
_SE_LAND = ("Landbaserat", "Statligt värdeautomater", "Fartyg")


def se_si(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "licences"):
        pk = _pk(r["operator"])
        lt = r["licence_type"] or ""
        # One row per website (online) or per site (land). The site and the
        # start date are part of a licence's identity — a renewal is a new
        # licence decision — and the website is not: three websites under one
        # online licence are one licence.
        lk = f"{pk}|{lt}|{r['site_name'] or ''}|{r['shop_id'] or ''}|{r['valid_from']}"
        prods = _SE_PRODUCTS.get(lt) or products(lt)
        notes = " · ".join(x for x in (r["note_type"], r["note"], r["note_url"]) if x) or None
        yield party(ref, pk, r["operator"])
        yield licence(ref, lk, party=pk, type=lt, products=prods,
                      channel="online" if lt == "Kommersiellt online" else ("land" if lt.startswith(_SE_LAND) else None),
                      site=r["site_name"], status_raw=r["status"], valid_from=r["valid_from"],
                      valid_to=r["valid_to"], notes=notes)
        if d := _domain(ref, lk, r["website"], party=pk, licence=lk, status_raw=r["status"], products=prods):
            yield d


def _sk_channel(game_types: str | None) -> str | None:
    s = (game_types or "").lower()
    online = "internet" in s
    land = any(w in s for w in ("v herni", "v kasíne", "kamenných"))
    return "both" if online and land else ("online" if online else ("land" if land else None))


def sk_urhh(t: Rows) -> Iterator[Fact]:
    for ref, r in _named(t, "licences"):
        pk = f"ico:{r['company_id']}" if r["company_id"] else _pk(r["company"])
        games = ",".join(sorted(g.strip() for g in (r["game_types"] or "").split(",") if g.strip()))
        yield party(ref, pk, r["company"], ico=r["company_id"])
        yield licence(ref, f"{pk}|{r['game_name'] or ''}|{games}|{r['valid_from']}", party=pk,
                      reference=",".join(r["game_codes"] or ()) or None, type=r["game_types"],
                      products=products(r["game_types"]), channel=_sk_channel(r["game_types"]),
                      valid_from=r["valid_from"], valid_to=r["valid_to"], notes=r["game_name"])


def us_nj_dge(t: Rows) -> Iterator[Fact]:
    # Sites are listed under the Atlantic City casino licensee that holds the
    # internet gaming permit, which is often not the brand's own company.
    for ref, r in _named(t, "internet_gaming_sites"):
        pk = _pk(r["licensee"])
        yield party(ref, pk, r["licensee"])
        if d := _domain(ref, pk, r["site"], party=pk, status_raw=r["status"]):
            yield d


PROJECTIONS: dict[str, Projection] = {
    f.__name__: f for f in (
        au_acma, be_gc, ca_kgc, ca_on_igo, ch_esbk, ch_gespa, cz_mf, de_ggl, ee_emta, es_dgoj, fr_anj,
        gb_ukgc, gr_hgc, ie_revenue, im_gsc, it_adm, pl_mf, pt_srij, se_si, sk_urhh, us_nj_dge,
    )
}


def project(slug: str, rows: Rows) -> list[Fact]:
    """Every fact one register's rows support. Facts without a key (a party
    whose name is empty) are dropped: they could never be linked or versioned."""
    return [f for f in PROJECTIONS[slug](rows) if f.key]
