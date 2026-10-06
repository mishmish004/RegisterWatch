"""What the data can answer, register by register — and where it cannot.

Every answer the model gives depends on what a register publishes. Germany's
whitelist names websites, so "this site is not on it" means something; Greece's
lists licensees without websites, so a domain can be neither confirmed nor
denied there; Switzerland is two blocklists and no licensee list at all here.
COVERAGE states that per register so verdicts can say which case they are in.

UNCOVERED is the other half: regulators that were checked and could not be
scraped (REGULATORS.md has the detail). An answer about Malta is "no data,
because the MGA's register is not machine-readable", not silence.

Only what the register modules and REGULATORS.md already establish is stated
here. Publication cadence is given where a register's own module documents it,
and is None otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Coverage:
    slug: str
    covers: frozenset[str]   # fact kinds the register supports: party, licence, brand, domain, block
    scope: str               # what the register lists, in one sentence
    cadence: str | None = None


def _c(slug: str, covers: str, scope: str, cadence: str | None = None) -> Coverage:
    return Coverage(slug, frozenset(covers.split()), scope, cadence)


COVERAGE: dict[str, Coverage] = {c.slug: c for c in (
    _c("au_acma", "party licence brand domain",
       "Interactive wagering providers licensed by an Australian state or territory regulator, with trading "
       "names and websites."),
    _c("be_gc", "party licence domain",
       "Every Belgian licence class; the online classes (A+, B+, F1+) list one website per licence.",
       "daily, around 04:30 CET"),
    _c("ca_kgc", "party licence domain",
       "Kahnawà:ke interactive permit holders with their websites, and casino software provider authorizations."),
    _c("ca_on_igo", "brand domain",
       "Operator brands live in Ontario's regulated iGaming market, with websites and offerings; no company "
       "names."),
    _c("ch_esbk", "block",
       "Blocklist of unauthorised online casino games (casino sector only).", "quarterly, as a dated PDF"),
    _c("ch_gespa", "block",
       "Blocklist of unauthorised lottery and sports-betting sites.", "dated releases"),
    _c("cz_mf", "party licence domain",
       "Legal operators under Act 186/2016 with company IDs; permits per game type and channel, with the "
       "domains internet permits cover.", "dated releases"),
    _c("de_ggl", "party licence domain",
       "The GGL whitelist: permitted operators, one permit per gambling type and authority, and the websites "
       "each permit covers."),
    _c("ee_emta", "party licence brand domain",
       "Legal gambling operators per subtype (online, casino, totalisator…) with brands and websites."),
    _c("es_dgoj", "party domain",
       "Operators holding a Spanish online gambling licence, with their websites; no licence detail."),
    _c("fr_anj", "party licence domain",
       "ANJ-approved online operators with their approval categories and websites."),
    _c("gb_ukgc", "party licence brand domain",
       "Every Gambling Commission licence with status and dates, plus each account's trading names and "
       "domain names.", "daily export, around 04:30 UTC"),
    _c("gr_hgc", "party licence",
       "Licensees by licence type; no websites."),
    _c("ie_revenue", "party licence brand",
       "Revenue's gaming licences and bookmakers' licences, with trading names; no websites."),
    _c("im_gsc", "party licence domain",
       "Online gambling licence holders with status and validity; websites where the register lists them.",
       "dated XLSX releases"),
    _c("it_adm", "block", "ADM's list of blocked gambling sites."),
    _c("pl_mf", "block",
       "Register of domains offering gambling unlawfully, with the date each was added."),
    _c("pt_srij", "party brand domain",
       "Licensed online brands with their websites and operating entities."),
    _c("se_si", "party licence domain",
       "Every Swedish licence, land-based and online, with status and validity; websites for online licences."),
    _c("sk_urhh", "party licence",
       "Individual licences per game with company IDs and validity; no websites."),
    _c("us_nj_dge", "party domain",
       "Internet gaming sites authorised in New Jersey, listed under the Atlantic City licensee holding the "
       "permit."),
)}


@dataclass(frozen=True)
class Uncovered:
    code: str
    name: str
    regulator: str
    reason: str


# REGULATORS.md, "Checked — not usable" (4 October 2026).
UNCOVERED: tuple[Uncovered, ...] = (
    Uncovered("MT", "Malta", "Malta Gaming Authority (MGA)",
              "The licensee register is an obfuscated client-side app with no export."),
    Uncovered("CW", "Curaçao", "Curaçao Gaming Authority (CGA)",
              "Client-side lookup only; no list or export."),
    Uncovered("NL", "Netherlands", "Kansspelautoriteit (KSA)",
              "Search interface only; no list or export."),
    Uncovered("DK", "Denmark", "Spillemyndigheden",
              "The licence-holder page is rendered client-side; no list or export."),
    Uncovered("GG", "Alderney", "Alderney Gambling Control Commission",
              "The list exists only inside a Wix site's client-side data."),
    Uncovered("GI", "Gibraltar", "Gibraltar Gambling Commissioner", "No public list of licensees found."),
    Uncovered("JE", "Jersey", "Jersey Gambling Commission", "Site unreachable when checked."),
    Uncovered("LT", "Lithuania", "Lošimų priežiūros tarnyba",
              "Behind a browser challenge; the open-data API returned errors."),
    Uncovered("RO", "Romania", "Oficiul Național pentru Jocuri de Noroc (ONJN)", "Behind a browser challenge."),
    Uncovered("LV", "Latvia", "Izložu un azartspēļu uzraudzības inspekcija (IAUI)",
              "The register was not located after the site moved."),
    Uncovered("CY", "Cyprus", "National Betting Authority",
              "Published as hundreds of ad-hoc dated files; no stable current file."),
    Uncovered("BR", "Brazil", "Secretaria de Prêmios e Apostas",
              "Dated PDFs at unpredictable URLs."),
    Uncovered("KE", "Kenya", "Betting Control and Licensing Board (BCLB)",
              "The licensed-operators page returned 404."),
    Uncovered("CO", "Colombia", "Coljuegos", "The authorised-operators page returned 404."),
    Uncovered("PE", "Peru", "MINCETUR", "No public list of authorised platforms located."),
    Uncovered("US-PA", "United States — Pennsylvania", "Pennsylvania Gaming Control Board",
              "Operator list is a PDF only; a candidate for the PDF approach."),
    Uncovered("US-MI", "United States — Michigan", "Michigan Gaming Control Board",
              "The authorised-operators page returned 404 / Access Denied."),
    Uncovered("UA", "Ukraine", "PlayCity", "No machine-readable register of licensees located."),
    Uncovered("MX", "Mexico", "SEGOB — Dirección General de Juegos y Sorteos", "The register host did not resolve."),
    Uncovered("PH", "Philippines", "PAGCOR", "Licensee lists are PDFs; the offshore list returned 404."),
    Uncovered("BG", "Bulgaria", "National Revenue Agency (NRA)", "Timed out when checked."),
    Uncovered("ZA", "South Africa — Western Cape", "Western Cape Gambling and Racing Board",
              "The page found did not contain the list."),
    Uncovered("SG", "Singapore", "Gambling Regulatory Authority", "No licensee list beyond category pages."),
)

# Ireland is covered through Revenue's registers; GRAI's own register, live
# from July 2026, is not yet published as data. Kept apart from UNCOVERED
# because IE is answered, just not by its new regulator.
PENDING: tuple[Uncovered, ...] = (
    Uncovered("IE", "Ireland", "Gambling Regulatory Authority of Ireland (GRAI)",
              "Licensing began 1 July 2026; the public register is not yet published as data."),
)


def coverage(slug: str) -> Coverage:
    return COVERAGE[slug]


def uncovered(code: str) -> Uncovered | None:
    return next((u for u in UNCOVERED if u.code == code.upper()), None)
