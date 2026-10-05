"""Every register's parser and discovery plan, against real published data.

Fixtures under fixtures/registers/<slug>/ are the regulators' own files as
downloaded in Oct 2026, gzipped; a few large ones (Poland's 58k-domain XML,
Italy's blocklist, UKGC's CSVs, Belgium's classes) are trimmed. `_index`,
`_item` are the pages the plans read.

When a regulator changes its layout, the live run fails with a stable code
(HEADER_MISMATCH, NO_TABLE…) and an incomplete snapshot; these tests are where
the fix gets pinned against the new file.
"""

from __future__ import annotations

import gzip
import pathlib
import re

import pytest

from registerwatch.ingest import validate
from registerwatch.ingest.engine import _shape
from registerwatch.registers import REGISTRY, all_registers
from registerwatch.registers.base import Bundle, DiscoveryError, ParseError

FIX = pathlib.Path(__file__).parent / "fixtures" / "registers"
TRIMMED = {"pl_mf", "it_adm", "gb_ukgc", "be_gc"}  # fixtures cut below min_rows on purpose


def fixture(slug: str, name: str) -> bytes:
    return gzip.decompress((FIX / slug / f"{name}.gz").read_bytes())


def bundle(slug: str) -> Bundle:
    return Bundle({p.name[:-3]: gzip.decompress(p.read_bytes())
                   for p in (FIX / slug).glob("*.gz") if not p.name.startswith("_")})


def parsed(slug: str) -> dict[str, list[dict]]:
    reg, b = REGISTRY[slug], bundle(slug)
    return {t.name: _shape(t, t.parse(b)) for t in reg.tables}


def test_twenty_one_registers_with_unique_slugs_and_fixtures():
    assert len(REGISTRY) == 21
    assert {p.name for p in FIX.iterdir()} == set(REGISTRY)


@pytest.mark.parametrize("slug", sorted(REGISTRY))
def test_every_table_parses_and_passes_its_own_checks(slug):
    reg = REGISTRY[slug]
    for table, rows in parsed(slug).items():
        t = next(x for x in reg.tables if x.name == table)
        check = validate.check_table(t, rows, baseline=None, tolerance=reg.count_delta_tolerance)
        problems = [p for p in check.problems if not (slug in TRIMMED and p.startswith("NO_ROWS"))]
        assert rows or t.min_rows == 0, f"{slug}.{table}: no rows"
        assert problems == [], f"{slug}.{table}: {problems}"
        assert check.warnings == [], f"{slug}.{table}: {check.warnings}"


# --- spot checks: one real row per register ------------------------------------

def _has(rows, **kw):
    return any(all(r.get(k) == v for k, v in kw.items()) for r in rows)


def test_gb_ukgc():
    t = parsed("gb_ukgc")
    assert _has(t["businesses"], account_number="67583", licence_account_name="1st Class Bet Ltd")
    lic = t["licences"][0]
    assert lic["licence_number"] == "000102-N-000000-000" and lic["status"] == "Pending"
    assert str(t["licences"][1]["start_date"]) == "2026-04-14"
    assert _has(t["domain_names"], domain_name="21.co.uk", host="21.co.uk")


def test_be_gc_splits_online_from_physical_licences():
    t = parsed("be_gc")
    assert _has(t["online_licences"], licence_class="BPLUS", website="www.madisoncasino.be")
    assert {r["licence_class"] for r in t["online_licences"]} == {"APLUS", "BPLUS", "FAPLUS"}
    assert _has(t["establishments"], licence_class="A", establishment="Grand Casino de Dinant", commune="Dinant")
    assert str(t["establishments"][0]["expiration_date"]) == "2038-01-19"


def test_de_ggl_one_row_per_permit_and_dates_per_site():
    t = parsed("de_ggl")
    bah = [r for r in t["permits"] if r["operator"] == "Bet-at-home.com Internet Limited"]
    assert {r["gambling_type"] for r in bah} == {"Sportwetten", "Virtuelle Automatenspiele"}
    assert "Mosta" in bah[0]["address"]
    site = next(r for r in t["websites"] if r["website"] == "bet-at-home.de" and r["gambling_type"] == "Sportwetten")
    assert str(site["first_permit_date"]) == "2020-11-02" and str(site["renewal_date"]) == "2022-12-22"


def test_es_dgoj():
    t = parsed("es_dgoj")
    assert _has(t["websites"], operator="888 ONLINE GAMES ESPAÑA, S.A.", host="www.888casino.es")
    assert len(t["operators"]) == 20  # two fixture pages of ten


def test_fr_anj_multi_site_operators():
    t = parsed("fr_anj")
    assert _has(t["websites"], operator="B.E.S. SAS", website="bwin.fr")
    assert _has(t["websites"], operator="B.E.S. SAS", website="partypoker.fr")
    assert _has(t["operators"], operator="Betclic Enterprises Limited",
                categories=["Paris sportifs", "Paris hippiques", "Jeux de cercle"])


def test_pt_srij():
    assert _has(parsed("pt_srij")["brands"], brand="Betclic", operator="BEM OPERATIONS LIMITED", host="www.betclic.pt")


def test_se_si():
    rows = parsed("se_si")["licences"]
    assert len(rows) > 2000
    online = [r for r in rows if r["licence_type"] == "Kommersiellt online"]
    assert online and all(r["operator"] for r in online)


def test_im_gsc_page_and_xlsx_list_the_same_licensees_spelled_differently():
    t = parsed("im_gsc")
    page = {r["company"] for r in t["licensees"]}
    xlsx = {r["name"] for r in t["licence_holders"]}
    assert len(page) == len(xlsx) == 53
    # The regulator's two publications disagree on four spellings. Both are
    # kept as published; matching them is the differ's job, not the parser's.
    assert page - xlsx == {"ST8 Innovations Limited", "RedPlay Limited", "Mamba Trading Limited", "HGIM"}
    assert xlsx - page == {"St8 Innovations Limited", "Redplay Limited", "Mamba Trading", "HGIM Ltd"}
    assert _has(t["domains"], company="Aceking IOM Limited", domain="www.kkpoker.net")
    assert not any(r["domain"] in ("-", "–") for r in t["domains"])
    assert _has(t["licensees"], company="Agreegain Limited") and \
        str(next(r for r in t["licensees"] if r["company"] == "Agreegain Limited")["valid_from"]) == "2025-05-08"


def test_ca_kgc_keeps_operators_without_url():
    t = parsed("ca_kgc")
    assert _has(t["operators"], operator="3-102-936774 SRL", url=None)
    assert _has(t["operators"], operator="3-102-939256 SRL", host="zumospin.com")
    assert len(t["software_providers"]) == 2


def test_ca_on_igo():
    assert _has(parsed("ca_on_igo")["brands"], brand="888 Casino", host="888casino.ca", offerings=["Casino"])


def test_pl_mf():
    rows = parsed("pl_mf")["blocked_domains"]
    assert rows[0] == {"entry_no": 58066, "domain": "www.azurslot6.com",
                       "added_at_local": "2026-10-02T11:32:03", "added_on": rows[0]["added_on"]}
    assert str(rows[0]["added_on"]) == "2026-10-02"


def test_it_adm_and_ch_gespa_skip_comments_and_dedupe():
    assert parsed("it_adm")["blocked_domains"][0]["domain"] == "01.mycoolcasino.com"
    g = parsed("ch_gespa")["blocked_domains"]
    assert not any(r["domain"].startswith("#") for r in g) and len(g) == len({r["domain"] for r in g})


def test_ch_esbk_reads_the_pdf():
    rows = parsed("ch_esbk")["blocked_domains"]
    assert len(rows) > 3000
    assert _has(rows, domain="5dimes.eu") and str(next(r for r in rows if r["domain"] == "5dimes.eu")["listed_on"]) == "2019-09-03"


def test_gr_hgc():
    assert _has(parsed("gr_hgc")["licensees"], company="BETMED LIMITED", licence_type="Betting (Type 1)")


def test_ee_emta_subtypes():
    t = parsed("ee_emta")
    assert {r["subtype"] for r in t["operators"]} >= {"online", "casino"}
    assert _has(t["websites"], operator="Aktsiaselts PAFER", website="www.speedybet.ee")


def test_cz_mf_matrix_becomes_permits():
    t = parsed("cz_mf")
    assert _has(t["operators"], operator="69GAMES a.s.", company_id="07597983")
    p = next(r for r in t["permits"] if r["operator"] == "3E Projekt, a.s." and r["channel"] == "Internet")
    assert p["game_type"] == "Technické hry" and p["domains"] == ["tokyo.cz"] and str(p["final_on"]) == "2023-12-30"
    # "Ú:3.8:2024" in the source — a typo — still reads as a date
    assert any(str(r["effective_on"]) == "2024-08-03" for r in t["permits"] if r["operator"] == "ALEKS CZ a.s.")


def test_us_nj_dge_splits_comma_separated_sites():
    rows = parsed("us_nj_dge")["internet_gaming_sites"]
    assert _has(rows, licensee="BORGATA HOTEL CASINO & SPA", site="nj.partypoker.com", status="authorized")
    assert _has(rows, licensee="BORGATA HOTEL CASINO & SPA", site="nj.partycasino.com")


def test_sk_urhh_skips_title_line():
    rows = parsed("sk_urhh")["licences"]
    assert rows[0]["seq"] == 1 and rows[0]["company_id"] == "00603741" and str(rows[0]["valid_to"]) == "2030-01-14"
    assert rows[0]["game_codes"] == ["93", "92", "95", "91", "97"]


def test_ie_revenue_strips_padded_cells():
    t = parsed("ie_revenue")
    assert t["gaming_licences"][0]["licence_type"] == "Gaming Licence Annual"
    assert _has(t["bookmakers"], licence_ref="WD0288", place_of_business="ON COURSE")


def test_au_acma():
    assert _has(parsed("au_acma")["providers"], trading_name="123bet", licence_holder="Winners Bookmaking Pty Ltd",
                licensing_authority="Liquor & Gaming NSW")


# --- layout changes fail loudly -------------------------------------------------

def test_a_moved_column_is_header_mismatch_not_shifted_data():
    reg = REGISTRY["gb_ukgc"]
    b = bundle("gb_ukgc")
    swapped = b["licences"].replace(b"Status,Type", b"Type,Status", 1)
    table = next(t for t in reg.tables if t.name == "licences")
    with pytest.raises(ParseError) as e:
        table.parse(Bundle({**dict(b), "licences": swapped}))
    assert e.value.code == "HEADER_MISMATCH"


@pytest.mark.parametrize("slug", ["de_ggl", "fr_anj", "pt_srij", "ca_on_igo", "us_nj_dge", "au_acma", "ee_emta"])
def test_an_html_page_without_the_register_raises(slug):
    reg = REGISTRY[slug]
    blank = Bundle({name: b"<html><body><p>Maintenance</p></body></html>" for name in bundle(slug)})
    for t in reg.tables:
        try:
            rows = t.parse(blank)
        except ParseError:
            continue
        assert len(rows) < t.min_rows, f"{slug}.{t.name} parsed a maintenance page into {len(rows)} rows"


# --- discovery plans ----------------------------------------------------------

def _fetcher(pages: dict[str, bytes]):
    def fetch(url: str) -> bytes:
        for pattern, body in pages.items():
            if re.fullmatch(pattern, url):
                return body
        raise DiscoveryError(url, 404)
    return fetch


PLANS = {
    "gb_ukgc": ({r".*/businesses/download": fixture("gb_ukgc", "_index")},
                {"businesses": r".*business-licence-register-businesses\.csv",
                 "licences": r".*business-licence-register-licences\.csv"}),
    "be_gc": ({r".*/licenses/": fixture("be_gc", "_index")},
              {"class-bplus": r"https://data\.gamingcommission\.be/licenses/BPLUS/latest/rows\.json"}),
    "es_dgoj": ({r".*/operadores": fixture("es_dgoj", "page-000")},
                {"page-000": r".*/operadores", "page-007": r".*/operadores\?page=7"}),
    "im_gsc": ({r".*online-gambling-licensee-register/": fixture("im_gsc", "register-page")},
               {"licence-holders": r"https://www\.isleofmangsc\.com/media/.*/26-10-02-current-ogra-licence-holders\.xlsx"}),
    "it_adm": ({r".*siti-web-inibiti-giochi": fixture("it_adm", "_index")},
               {"blocklist": r"https://www\.adm\.gov\.it/portale/documents/.*/elenco_siti_inibiti_giochi\.txt/.*"}),
    "ch_gespa": ({r"https://blocklist\.gespa\.ch/": fixture("ch_gespa", "_index")},
                 {"blocklist": r"https://blocklist\.gespa\.ch/gespa_blocklist_20260825\.txt"}),
    "ch_esbk": ({r".*unauthorised-online-games": fixture("ch_esbk", "_index")},
                {"blocklist": r".*sperrliste-2026-08-25-dfi\.pdf"}),
    # The index lists a newer page (…-65357) than the item fixture; the plan
    # must pick the highest id, whatever page body comes back for it.
    "cz_mf": ({r".*whiteli": fixture("cz_mf", "_index"), r".*-65357": fixture("cz_mf", "_item")},
              {"operators": r"https://mf\.gov\.cz/assets/attachments/2026-02-26_Prehled-provozovatelu-ZHH\.xlsx"}),
    "sk_urhh": ({r".*zoznam-udelenych-individualnych-licencii/": fixture("sk_urhh", "_index")},
                {"licences": r"https://www\.urhh\.sk/wp-content/uploads/urhh/5/.*\.CSV"}),
}


@pytest.mark.parametrize("slug", sorted(PLANS))
def test_plan_discovers_the_newest_files(slug):
    pages, expect = PLANS[slug]
    parts = {p.name: p for p in REGISTRY[slug].resolve_parts(_fetcher(pages))}
    for name, url_pattern in expect.items():
        assert parts[name].url and re.fullmatch(url_pattern, parts[name].url), (name, parts[name].url)


def test_a_plan_whose_index_lost_the_link_marks_the_part_not_listed():
    for slug in ("it_adm", "ch_gespa", "ch_esbk", "sk_urhh", "im_gsc"):
        parts = REGISTRY[slug].resolve_parts(lambda url: b"<html><body>moved</body></html>")
        assert any(p.url is None and p.expected for p in parts), slug


def test_every_register_is_described_for_the_record():
    for r in all_registers():
        assert r.homepage.startswith("https://") and r.country and r.regulator
        assert (r.plan is not None) != bool(r.parts)
