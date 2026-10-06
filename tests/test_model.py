"""The model, without a database: names, hosts, vocabularies, every register's
projection against its fixture, the build's versions and events, and the domain
verdicts. db/repos/model.py and test_postgres.py own the SQL side."""

from __future__ import annotations

import pathlib
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pytest

from registerwatch.model import catalogue, domains, facts, names
from registerwatch.model.build import RegisterInput, build
from registerwatch.model.facts import RowRef
from registerwatch.model.projections import PROJECTIONS, project
from registerwatch.model.verdict import assess, relation
from registerwatch.registers import REGISTRY, all_registers
from tests.test_registers import TRIMMED, parsed

ROOT = pathlib.Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 1, 6, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)
T2 = T0 + timedelta(days=2)
TIMES = {1: T0, 2: T1, 3: T2}


def ref(table: str, row_id: int, first: int = 1, removed: int | None = None) -> RowRef:
    return RowRef(table, row_id, first, removed, TIMES[first], TIMES[removed or first], TIMES.get(removed))


def loaded(slug: str) -> dict[str, list]:
    """A register's fixture rows as one complete first snapshot."""
    return {t: [(ref(t, i), r) for i, r in enumerate(v)] for t, v in parsed(slug).items()}


@pytest.fixture(scope="module")
def fixture_model():
    return build([RegisterInput(REGISTRY[s], loaded(s), TIMES) for s in sorted(REGISTRY)])


# --- names and hosts -----------------------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("RedPlay Limited", "Redplay Limited"),                          # the Isle of Man's page vs its XLSX
    ("Codere Apuestas España S.L.U.", "CODERE APUESTAS ESPANA SLU"),
    ("Tipico Co. Ltd.", "Tipico Co Ltd"),
    ("COALQUAY LEISURE LIMITED", "COALQUAY LEISURE LTD"),
    ("BALLY’S ATLANTIC CITY", "Bally's Atlantic City"),
])
def test_spellings_of_one_name_share_a_key(a, b):
    assert names.name_key(a) == names.name_key(b)


@pytest.mark.parametrize("name, key, core", [
    ("Hillside (Gibraltar) Limited", "hillside gibraltar ltd", "hillside gibraltar"),
    ("N I K É , spol. s r.o.", "nike sro", "nike"),
    ("STS Sp. z o.o.", "sts spzoo", "sts"),
    ("Aktsiaselts PAFER", "as pafer", "as pafer"),     # a leading legal form is part of the name
    ("3-102-939256 SRL", "3 102 939256 srl", "3 102 939256"),
])
def test_name_key_and_core(name, key, core):
    assert names.name_key(name) == key and names.name_core(key) == core


def test_different_legal_forms_are_different_keys_but_one_core():
    a, b = names.name_key("Betway Limited"), names.name_key("Betway SA")
    assert a != b and names.name_core(a) == names.name_core(b) == "betway"


@pytest.mark.parametrize("value, host, registrable, label", [
    ("https://www.Betclic.pt/", "betclic.pt", "betclic.pt", "betclic"),
    ("www.bet365.co.uk", "bet365.co.uk", "bet365.co.uk", "bet365"),
    ("nj.bet365.com/casino", "nj.bet365.com", "bet365.com", "bet365"),
    ("foo.herokuapp.com", "foo.herokuapp.com", "foo.herokuapp.com", "foo"),  # private suffixes count
    ("http://1.2.3.4/x", "1.2.3.4", None, None),
    ("No current URL", None, None, None),
])
def test_hosts(value, host, registrable, label):
    h = domains.host_key(value)
    assert (h, domains.registrable(h), domains.label(h)) == (host, registrable, label)


def test_parents_stop_at_the_registrable_domain():
    assert domains.parents("a.b.example.co.uk") == ["a.b.example.co.uk", "b.example.co.uk", "example.co.uk"]


@pytest.mark.parametrize("asked, other, rel", [
    ("bet365.com", "bet365.com", "exact"), ("a.bet365.com", "bet365.com", "parent"),
    ("bet365.com", "nj.bet365.com", "child"), ("casino.bet365.com", "nj.bet365.com", "sibling"),
    ("bet365.com", "bet365.es", None), ("bet365.com", None, None),
])
def test_relation(asked, other, rel):
    assert relation(asked, other) == rel


# --- vocabularies -------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, normalised, since", [
    ("Active", "active", None), ("Aktiv", "active", None), ("Licenced", "active", None),
    ("Inaktiv", "inactive", None), ("Revoked - Non Payment of Fee", "revoked", None),
    ("Suspended (2 October 2026)", "suspended", date(2026, 10, 2)),
    ("Suspended (02/10/2026)", "suspended", date(2026, 10, 2)),
    ("White Label", "white_label", None), (None, "listed", None), ("Something new", "unknown", None),
])
def test_status(raw, normalised, since):
    assert facts.status(raw) == (normalised, since)


@pytest.mark.parametrize("text, tags", [
    ("General Betting Limited", ("betting",)),
    ("Linked Gaming Machine Technical Supplier", ("gaming_machines", "b2b")),
    ("Casino 1968 Act", ("casino",)),
    ("Virtuelle Automatenspiele", ("casino",)),
    ("Pferdewetten im Internet", ("betting", "horse_racing")),
    ("Paris hippiques", ("betting", "horse_racing")),
    ("Jeux de cercle", ("poker",)),
    ("Kursové sázky", ("betting",)),
    ("Statligt värdeautomater", ("gaming_machines",)),
    ("Lotériové hry / okamžité lotérie", ("lottery",)),
    ("Full", ()),          # the Isle of Man's "Full" licence names no product: none is guessed
])
def test_products(text, tags):
    assert facts.products(text) == tags


@pytest.mark.parametrize("raw, ch", [("Remote", "online"), ("Non-Remote", "land"), ("Ancillary Remote", "online"),
                                     ("Land-Based", "land"), ("Hybrid", "both"), ("online/stationär", "both"),
                                     (None, None)])
def test_channel(raw, ch):
    assert facts.channel(raw) == ch


# --- projections ----------------------------------------------------------------------------

def test_every_register_has_a_projection_and_a_coverage_entry():
    assert set(PROJECTIONS) == set(REGISTRY) == set(catalogue.COVERAGE)


@pytest.mark.parametrize("slug", sorted(REGISTRY))
def test_projection_yields_exactly_what_the_catalogue_says_it_covers(slug):
    fs = project(slug, loaded(slug))
    assert {f.kind for f in fs} == catalogue.COVERAGE[slug].covers
    pools = {k: {f.key for f in fs if f.kind == k} for k in ("party", "licence", "brand")}
    dangling = [(f.kind, link) for f in fs for link in ("party", "licence", "brand")
                if link in dict(f.values) and f.get(link) and f.get(link) not in pools[link]]
    # Trimmed fixtures cut each file separately, so their links can point at cut rows.
    assert slug in TRIMMED or not dangling, dangling[:5]


def test_ukgc_licence_identity_leaves_out_the_version_segment():
    lic = next(f for f in project("gb_ukgc", loaded("gb_ukgc")) if f.kind == "licence"
               and f.get("reference") == "000102-N-317976-010")
    assert lic.key == "acct:102|000102-N-317976|Bingo"


def test_sweden_collapses_websites_of_one_online_licence():
    fs = [f for f in project("se_si", loaded("se_si")) if f.kind == "licence" and f.key.startswith("name:prozone ltd|")]
    online = {f.key for f in fs if f.get("type") == "Kommersiellt online"}
    assert len(online) == 1 and sum(f.get("type") == "Kommersiellt online" for f in fs) == 3


def test_individuals_named_beside_a_licence_are_not_projected():
    fs = project("ie_revenue", loaded("ie_revenue"))
    officers = {r["relevant_officer"] for r in parsed("ie_revenue")["gaming_licences"] if r["relevant_officer"]}
    projected = {str(v) for f in fs for _, v in f.values}
    assert not officers & projected


# --- build ------------------------------------------------------------------------------------

def test_the_first_snapshot_is_a_baseline_not_news(fixture_model):
    assert fixture_model.events == []
    assert len(fixture_model.parties) > 2000 and len(fixture_model.blocks) > 5000


def test_model_rows_link_and_normalise(fixture_model):
    se = next(d for d in fixture_model.domains if d["register"] == "se_si" and d["host"] == "fastbet.com")
    lic = next(x for x in fixture_model.licences if x["licence_id"] == se["licence_id"])
    party = next(p for p in fixture_model.parties if p["party_id"] == se["party_id"])
    assert (se["status"], lic["status"], lic["products"], party["name"]) == ("active", "active", ["casino"],
                                                                              "Prozone Limited")
    im = next(x for x in fixture_model.licences if x["status"] == "suspended")
    assert im["status_raw"] == "Suspended (2 October 2026)" and im["status_since"] == date(2026, 10, 2)
    assert all(e["table"] for row in fixture_model.licences for e in row["evidence"])


def _gb_history():
    """GB over three snapshots: renumber one licence, revoke another, drop a
    third, add a fourth, rename an account, and touch a row cosmetically."""
    rows = loaded("gb_ukgc")
    lic, biz = rows["licences"], rows["businesses"]
    a, b, c = (dict(lic[i][1]) for i in (1, 2, 3))
    lic[1] = (ref("licences", 1, 1, 2), a)
    lic.append((ref("licences", 901, 2), {**a, "licence_number": a["licence_number"][:-3] + "011"}))
    lic[2] = (ref("licences", 2, 1, 2), b)
    lic.append((ref("licences", 902, 2), {**b, "status": "Revoked"}))
    lic[3] = (ref("licences", 3, 1, 3), c)
    lic.append((ref("licences", 903, 3), {**c, "licence_number": "000102-R-999999-001"}))
    p = dict(biz[0][1])
    biz[0] = (ref("businesses", 0, 1, 2), p)
    biz.append((ref("businesses", 900, 2), {**p, "licence_account_name": p["licence_account_name"] + " (Renamed)"}))
    return rows, (a, b, c, p)


def test_versions_become_one_row_and_changes_become_typed_events():
    rows, (a, b, c, p) = _gb_history()
    m = build([RegisterInput(REGISTRY["gb_ukgc"], rows, TIMES)])
    got = Counter((e["type"], e["at"]) for e in m.events)
    assert got == Counter({("licence.changed", T1): 1, ("licence.status_changed", T1): 1,
                           ("party.changed", T1): 1, ("licence.removed", T2): 1, ("licence.added", T2): 1})
    renumbered = next(e for e in m.events if e["type"] == "licence.changed")
    assert renumbered["before"] == {"reference": a["licence_number"]}
    assert renumbered["after"] == {"reference": a["licence_number"][:-3] + "011"}
    revoked = next(e for e in m.events if e["type"] == "licence.status_changed")
    assert (revoked["before"], revoked["after"]) == ({"status_raw": "Active"}, {"status_raw": "Revoked"})
    assert "status Active → Revoked" in revoked["summary"]
    row = next(x for x in m.licences if x["reference"] == a["licence_number"][:-3] + "011")
    assert row["versions"] == 2 and row["current"] and row["first_seen_at"] == T0
    gone = next(x for x in m.licences if x["reference"] == c["licence_number"])
    assert not gone["current"] and gone["removed_at"] == T2


def test_a_row_change_no_fact_reads_is_not_an_event():
    rows = loaded("se_si")
    r0 = dict(rows["licences"][0][1])
    rows["licences"][0] = (ref("licences", 0, 1, 2), r0)
    rows["licences"].append((ref("licences", 9000, 2), {**r0, "address_1": "Somewhere else 1"}))
    m = build([RegisterInput(REGISTRY["se_si"], rows, TIMES)])
    assert m.events == []


def test_blocklist_removal_and_return():
    rows = loaded("it_adm")
    r0 = rows["blocked_domains"][0][1]
    rows["blocked_domains"][0] = (ref("blocked_domains", 0, 1, 2), r0)
    rows["blocked_domains"].append((ref("blocked_domains", 900, 3), dict(r0)))
    m = build([RegisterInput(REGISTRY["it_adm"], rows, TIMES)])
    assert [(e["type"], e["at"]) for e in m.events] == [("block.removed", T1), ("block.added", T2)]
    assert "removed from the blocklist" in m.events[0]["summary"]


def test_a_table_first_loaded_later_is_its_own_baseline():
    rows = loaded("ch_gespa")
    rows["blocked_domains"] = [(ref("blocked_domains", i, 2), r) for i, (_, r) in enumerate(rows["blocked_domains"])]
    assert build([RegisterInput(REGISTRY["ch_gespa"], rows, TIMES)]).events == []


def test_rebuilding_gives_the_same_ids():
    rows, _ = _gb_history()
    one, two = (build([RegisterInput(REGISTRY["gb_ukgc"], rows, TIMES)]) for _ in range(2))
    assert [e["id"] for e in one.events] == [e["id"] for e in two.events]
    assert [x["licence_id"] for x in one.licences] == [x["licence_id"] for x in two.licences]


# --- verdicts -------------------------------------------------------------------------------

def _assess(model, host, *, today=date(2026, 10, 6), extra_listings=(), extra_blocks=(), extra_licences=()):
    listings = [d for d in model.domains if relation(host, d["host"])] + list(extra_listings)
    blocks = [b for b in model.blocks if relation(host, b["host"])] + list(extra_blocks)
    lics = {x["licence_id"]: x for x in [*model.licences, *extra_licences]}
    return assess(host, listings=listings, blocks=blocks, licences=lics,
                  parties={p["party_id"]: p for p in model.parties}, brands={b["brand_id"]: b for b in model.brands},
                  registers=all_registers(), today=today)


def _by_jur(res):
    return {v["jurisdiction"]: v for v in res["verdicts"]}


def test_one_domain_many_answers(fixture_model):
    res = _assess(fixture_model, "bet365.com")
    v = _by_jur(res)
    assert res["authorised_in"] == ["GB", "SE"] and res["blocked_in"] == ["CH"]
    assert v["US-NJ"]["verdict"] == "related_listed"            # nj.bet365.com is, bet365.com is not
    assert v["GR"]["verdict"] == "no_domain_data"               # Greece lists no websites
    assert v["IT"]["verdict"] == "not_blocked" and v["AU"]["verdict"] == "not_listed"
    assert v["SE"]["confidence"] == "high" and "Hillside (Europe) ENC" in v["SE"]["explanation"]
    assert v["CH"]["matches"] and all(m["evidence"] for m in v["CH"]["matches"])


def _listing(**kw):
    base = {"listing_id": "x", "register": "se_si", "jurisdiction": "SE", "host": "example.com",
            "published": "example.com", "party_id": None, "brand_id": None, "licence_id": None,
            "status": "active", "status_raw": "Aktiv", "products": [], "since": None, "current": True,
            "first_seen_at": T0, "removed_at": None, "evidence": [{"table": "licences", "row_id": 1}]}
    return {**base, **kw}


def _licence(**kw):
    base = {"licence_id": "se_si:L", "register": "se_si", "jurisdiction": "SE", "party_id": None,
            "regulator": "Spelinspektionen", "authority": "Spelinspektionen", "reference": "L-1", "type": "t",
            "products": [], "status": "active", "status_raw": "Aktiv", "status_since": None,
            "valid_from": None, "valid_to": None, "area": None, "current": True, "removed_at": None}
    return {**base, **kw}


def _block(**kw):
    base = {"block_id": "b", "register": "it_adm", "jurisdiction": "IT", "regulator": "ADM", "host": "example.com",
            "published": "example.com", "listed_on": None, "current": True, "first_seen_at": T0, "removed_at": None,
            "evidence": []}
    return {**base, **kw}


@pytest.mark.parametrize("listing, licence, verdict, confidence", [
    (_listing(licence_id="se_si:L"), _licence(status="suspended", status_raw="Suspended"),
     "listed_not_operating", "high"),
    (_listing(licence_id="se_si:L"), _licence(valid_to=date(2026, 1, 1)), "authorised", "medium"),
    (_listing(status="inactive", status_raw="Inaktiv"), None, "listed_not_operating", "high"),
    (_listing(status="white_label", status_raw="White Label"), None, "authorised", "medium"),
    (_listing(current=False, removed_at=T1), None, "previously_listed", "medium"),
    (_listing(host="nj.example.com"), None, "related_listed", "low"),
])
def test_grey_areas_are_not_flattened(fixture_model, listing, licence, verdict, confidence):
    v = _by_jur(_assess(fixture_model, "example.com", extra_listings=[listing],
                        extra_licences=[licence] if licence else []))["SE"]
    assert (v["verdict"], v["confidence"]) == (verdict, confidence), v["explanation"]


def test_a_party_with_no_active_licence_is_not_authorised(fixture_model):
    lic = _licence(party_id="se_si:P", status="revoked", status_raw="Revoked")
    v = _by_jur(_assess(fixture_model, "example.com", extra_listings=[_listing(party_id="se_si:P")],
                        extra_licences=[lic]))["SE"]
    assert v["verdict"] == "listed_not_operating" and "none of the party's licences" in v["explanation"]


def test_listed_and_blocked_in_one_jurisdiction_is_a_conflict(fixture_model):
    res = _assess(fixture_model, "example.com", extra_listings=[_listing(register="it_adm", jurisdiction="IT")],
                  extra_blocks=[_block()])
    assert _by_jur(res)["IT"]["verdict"] == "conflict" and "IT" in res["blocked_in"] and "IT" in res["attention"]


def test_a_blocked_parent_and_a_lifted_block(fixture_model):
    v = _by_jur(_assess(fixture_model, "play.example.com", extra_blocks=[_block()]))["IT"]
    assert v["verdict"] == "blocked_parent" and v["confidence"] == "medium"
    v = _by_jur(_assess(fixture_model, "example.com", extra_blocks=[_block(current=False, removed_at=T1)]))["IT"]
    assert v["verdict"] == "previously_blocked"


# --- catalogue ---------------------------------------------------------------------------------

def test_uncovered_jurisdictions_are_the_ones_regulators_md_records():
    doc = (ROOT / "REGULATORS.md").read_text()
    for u in (*catalogue.UNCOVERED, *catalogue.PENDING):
        assert u.regulator.split(" (")[0] in doc, u
    assert not {u.code for u in catalogue.UNCOVERED} & {r.country for r in all_registers()}
