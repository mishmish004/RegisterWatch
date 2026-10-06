# Gambling regulators: what is scraped, and what is not

Checked 4 October 2026. A register is **scraped** when its data can be fetched
reliably over plain HTTP: a file, an API, or a server-rendered page, with no
login, no CAPTCHA or browser challenge, and no search-one-name-at-a-time
interface. Everything else is listed below with what was actually found, so
"not scraped" is a recorded decision rather than an oversight.

## Scraped — 21 registers, one Postgres schema each

| Country | Regulator | What | Schema · tables | Source |
|---|---|---|---|---|
| AU | Australian Communications and Media Authority (ACMA) | Licensed interactive wagering providers | `au_acma` · providers | HTML table |
| BE | Kansspelcommissie / Commission des jeux de hasard | All licence classes (A–GA, A+/B+/F1+) | `be_gc` · online_licences, establishments | JSON per class |
| CA | Kahnawà:ke Gaming Commission | Interactive permit holders | `ca_kgc` · operators, software_providers | HTML tables |
| CA-ON | iGaming Ontario | Regulated market operator brands | `ca_on_igo` · brands | HTML |
| CH | Eidgenössische Spielbankenkommission (ESBK) | Casino-games blocklist | `ch_esbk` · blocked_domains | PDF (newest, discovered) |
| CH | Gespa | Lottery/betting blocklist | `ch_gespa` · blocked_domains | TXT (newest, discovered) |
| CZ | Ministerstvo financí | Legal gambling operators (ZHH) | `cz_mf` · operators, permits | XLSX (via dated page) |
| DE | Gemeinsame Glücksspielbehörde der Länder (GGL) | Whitelist of permitted operators | `de_ggl` · permits, websites | HTML |
| EE | Maksu- ja Tolliamet (EMTA) | Legal gambling operators | `ee_emta` · operators, websites | HTML tables |
| ES | Dirección General de Ordenación del Juego (DGOJ) | Licensed online operators | `es_dgoj` · operators, websites | Paginated HTML |
| FR | Autorité nationale des jeux (ANJ) | Approved online operators | `fr_anj` · operators, websites | HTML |
| GB | Gambling Commission | Public register of businesses | `gb_ukgc` · businesses, licences, trading_names, domain_names | 4 CSVs (discovered) |
| GR | Hellenic Gaming Commission (ΕΕΕΠ) | Licensees | `gr_hgc` · licensees | XLSX |
| IE | Revenue Commissioners | Gaming licences, bookmakers | `ie_revenue` · gaming_licences, bookmakers | 2 CSVs |
| IM | Gambling Supervision Commission | Online gambling licence holders | `im_gsc` · licensees, domains, licence_holders | HTML + dated XLSX |
| IT | Agenzia delle Dogane e dei Monopoli (ADM) | Blocked gambling sites | `it_adm` · blocked_domains | TXT (discovered) |
| PL | Ministerstwo Finansów | Register of unlawful gambling domains | `pl_mf` · blocked_domains | XML API |
| PT | Serviço de Regulação e Inspeção de Jogos (SRIJ) | Licensed online entities | `pt_srij` · brands | HTML |
| SE | Spelinspektionen | Licence register | `se_si` · licences | XLSX export endpoint |
| SK | Úrad pre reguláciu hazardných hier (ÚRHH) | Individual licences | `sk_urhh` · licences | CSV (discovered) |
| US-NJ | Division of Gaming Enforcement | Authorised internet gaming sites | `us_nj_dge` · internet_gaming_sites | HTML |

All 21 completed live runs into the production database on 4 Oct 2026. Two
needed something specific:

- **ACMA** answered user agents containing "compliance change monitoring" with
  its 59-byte uptime-check JSON most of the time, and every other wording with
  the register. The default user agent avoids the word "monitoring", and the
  engine retries once in the same run when a page comes back as the wrong kind
  of content.
- **Poland's** XML (~9 MB) is generated per request and took longer than the
  default 30 s timeout; the register sets 180 s.

## Checked — not usable

| Country | Regulator | What was found |
|---|---|---|
| MT | Malta Gaming Authority (MGA) | The licensee register is an Angular app (`mgalicenseeregister.mga.org.mt`) with a deliberately obfuscated bundle (every string hex-escaped) and no export. The Malta open-data portal entry for the register is behind a Cloudflare challenge (403). |
| CW | Curaçao Gaming Authority (CGA) | `cert.cga.cw` is a client-side lookup with no list or export; `portal.gamingcontrolcuracao.org` did not resolve. |
| NL | Kansspelautoriteit (KSA) | The Kansspelwijzer is a search interface; results are not in the page and there is no export. |
| DK | Spillemyndigheden | The licence-holder page is rendered client-side (Next.js) from tagged content; no list or export in the response. |
| GG | Alderney Gambling Control Commission | Wix site; the list exists only inside Wix's client-side data. |
| GI | Gibraltar Gambling Commissioner | No public list of licensees found on gibraltar.gov.gi. |
| JE | Jersey Gambling Commission | Site unreachable (connection timeout) when checked. |
| LT | Lošimų priežiūros tarnyba | `lpt.lrv.lt` is behind a Cloudflare browser challenge; the open-data portal (data.gov.lt) API returned 500. Worth re-checking the open-data route. |
| RO | Oficiul Național pentru Jocuri de Noroc (ONJN) | `onjn.gov.ro` serves a "Verifying your browser" challenge (503). |
| LV | Izložu un azartspēļu uzraudzības inspekcija (IAUI) | `iaui.gov.lv` now redirects to the State Revenue Service (VID) homepage; the register was not located there. |
| CY | National Betting Authority | Class A register published as ~300 ad-hoc dated `.xls` files with inconsistent names; Class B as one PDF per licence. No stable "current" file. |
| BR | Secretaria de Prêmios e Apostas (Ministério da Fazenda) | Authorised operators published as dated PDFs (`planilha-de-autorizacoes-DD-MM-YYYY.pdf`) at unpredictable URLs; the listing page now redirects to a page without the links. |
| KE | Betting Control and Licensing Board (BCLB) | `licensed-operators` page returned 404; the server's TLS chain is incomplete. |
| CO | Coljuegos | The authorised-online-operators page returned 404; the open-data page has no operator list. |
| PE | MINCETUR | No public list of authorised platforms located. |
| US-PA | Pennsylvania Gaming Control Board | Operator list published only as PDF (`Online_Operator_Master_List.pdf`). **Candidate**: the PDF approach used for ESBK would work. |
| US-MI | Michigan Gaming Control Board | Authorised-operators page returned 404 / Access Denied. |
| IE | Gambling Regulatory Authority of Ireland (GRAI) | Licensing began 1 July 2026; the public register is not yet published as data. Revenue's registers are scraped meanwhile (`ie_revenue`). |
| UA | PlayCity | `licenses.pc.gov.ua` holds licensing-procedure content only; no machine-readable register of licensees located. |
| MX | SEGOB — Dirección General de Juegos y Sorteos | `sijscasinos.segob.gob.mx` did not resolve. |
| PH | PAGCOR | Licensee lists are PDFs; the offshore-licensee PDF URL now returns 404. |
| BG | National Revenue Agency (NRA) | `nra.bg` timed out. |
| ZA | Western Cape Gambling and Racing Board | The licence-holders page found did not contain the list; other provincial boards not assessed. |
| SG | Gambling Regulatory Authority | No licensee list beyond category pages (the exempt operators are three named entities). |

## Not assessed

Not checked in this pass; listed so the gap is visible.

- **State monopolies with no licensee register to watch**: Norway (Lotteritilsynet), Finland (licensing opens 2027), Austria, Iceland, Hungary (largely), New Zealand, and the Canadian provinces other than Ontario (BCLC, Loto-Québec, Atlantic Lottery, AGLC…).
- **Other US states**: NY, IL, CO, IN, IA, KS, KY, LA, MD, MA, NH, NC, OH, OR, RI, TN, VA, WV, WY, AZ, AR, CT, DE, DC, VT, ME, NV.
- **Latin America**: Argentina (provincial regulators: LOTBA, IPLyC…), Chile, Paraguay, Ecuador, Panama, Costa Rica.
- **Europe**: Croatia, Serbia, Slovenia, Montenegro, North Macedonia, Bosnia and Herzegovina, Albania, Armenia, Georgia, Moldova.
- **Asia**: Macau (DICJ: six concessionaires), Cambodia, Japan, India (state-level), South Korea, Kazakhstan.
- **Africa**: Nigeria (NLRC, Lagos LSLGA), Ghana, Tanzania, Uganda, Zambia, Malawi, Namibia, the other South African provincial boards.
- **Offshore**: Anjouan.

## Adding a register

One module under `src/registerwatch/registers/` building a `Register` (URLs or
a discovery plan, and tables with parsers), one line in `registers/__init__.py`,
a fixture under `tests/fixtures/registers/<slug>/`, then
`uv run registerwatch ddl --write` and `uv run registerwatch migrate`. For the
model, add a projection and a coverage entry (see [docs/MODEL.md](docs/MODEL.md));
a regulator moving from "Checked" to "Scraped" also leaves
`model/catalogue.py`'s `UNCOVERED`.
