# The model: 21 registers as one set of answers

[research-plan.md](research-plan.md) describes the product: who is licensed,
under which licence, for which brands and websites, in which jurisdiction, by
which regulator. It also asks what changed and when, where the evidence is, and
where the answer is neither yes nor no. The register schemas keep each
regulator's rows as published. The model is the layer that answers those
questions across all registers at once.

It is derived data. `registerwatch build` (and every ingest batch that recorded a
complete snapshot) rebuilds the `model` schema from the register schemas in one
transaction, in about two seconds. Nothing in it is typed in by hand. Every row
names the register rows behind it.

```
party ──holds──▶ licence ──lists──▶ domain listing ◀── same host · parent · label ──▶ block entry
  │                                       ▲
  └──trades as──▶ brand ──────────────────┘

every fact belongs to a register → its regulator → its jurisdiction
every fact has first seen · last seen · removed · versions · evidence (the register rows)
every change between versions of a fact is an event
```

## Five kinds of fact

| kind | what it is | `model.` table | id |
|---|---|---|---|
| party | a legal entity as one register names it | `parties` | `gb_ukgc:acct:39198`, `cz_mf:ico:25389092`, `de_ggl:name:tipico co ltd` |
| licence | a permission a register says a party holds | `licences` | `gb_ukgc:acct:102\|000102-N-317976\|Bingo` |
| brand | a trading name | `brands` | `pt_srij:name:bem operations ltd\|betclic` |
| domain | one website entry on a licensee register | `domains` | `se_si:…\|fastbet.com` |
| block | one entry on a blocklist | `blocks` | `pl_mf:www.azurslot6.com` |

A register only gets the kinds it actually publishes
(`model/catalogue.py`, which a test holds to the projections). Spain lists
operators and websites without licence detail, so ES has no licence facts.
Ontario lists brands without companies, so CA-ON has no parties. Greece, Ireland
and Slovakia list no websites, so a domain there gets `no_domain_data`, never a
guess.

**Operators.** Parties are clustered on `name_key`, which ignores accents,
case and punctuation and spells legal forms one way ("Limited" → `ltd`,
"S.L.U." → `slu`). "Hillside (New Media Malta) PLC" in Estonia and
"HILLSIDE (New Media Malta) PLC" in France become one operator,
`hillside-new-media-malta-plc`, with both register parties kept underneath.
Different legal forms stay apart: "Betway Limited" and "Betway Spain, S.A." are
two operators. Search finds both through the shared core "betway".

**Identity is where most of the judgement sits.** Each projection
(`model/projections.py`) decides which rows are versions of one thing:

| register | licence identity | why |
|---|---|---|
| gb_ukgc | account + licence number without its last segment + activity | the last segment is a version counter (~480 of ~4,500 rows moved in one week) |
| se_si | operator + type + site + start date | one row per website under one online licence; a renewal is a new decision |
| de_ggl | operator + gambling type + authority + sales area | a Land authority's permit covers that Land |
| cz_mf | IČO + game type + channel | the sheet is a matrix of permits |
| be_gc | class + dossier | |

A domain listing's identity is the entry as published under its licence (or
party). `berriez.com/nz` and `berriez.com/en` are two listings of one host.
A block's identity is its published entry. Poland lists `www.x.com` and `x.com`
separately.

**Vocabularies.** Status is normalised beside the published value:
`active, pending, suspended, revoked, surrendered, expired, lapsed, forfeited,
inactive, white_label, listed, unknown`. `listed` means the register states no
status and being on it is the status. `unknown` means a value nobody has mapped
yet. Products are `casino, betting, horse_racing, poker, bingo, lottery,
gaming_machines, b2b`, read from licence types in each register's language.
Ireland's officers and other named individuals beside a licence are not
projected.

## Events: what changed, and when

For every fact, the build walks the snapshots where its versions changed. At
each one it compares the values current before and after:

| before → after | event |
|---|---|
| nothing → something | `<kind>.added` |
| something → nothing | `<kind>.removed` |
| status differs | `<kind>.status_changed` |
| anything else differs | `<kind>.changed`, with the fields before and after |

Comparing values instead of rows keeps the feed quiet. A UKGC renumbering is
one `licence.changed` (reference …-010 → …-011), not a revocation and a grant.
A change to a column no fact reads, such as a Swedish shop's street address, is
no event at all. A register's first snapshot is its baseline, so watching
starting is not reported as 4,000 new licences. Event ids are stable across
rebuilds.

```
$ registerwatch events --since 2026-10-01        # summaries column
GB · Gambling Commission: licence 000300-A-104099-007 (Casino) — status Active → Suspended
GB · Gambling Commission: licence 000102-N-317976-011 (Bingo) — reference 000102-N-317976-010 → 000102-N-317976-011
IM · Gambling Supervision Commission: licence (Network Services) of Aceking IOM Limited — status Active → Suspended (5 October 2026)
IT · Agenzia delle Dogane e dei Monopoli (ADM): betway.com — added to the blocklist
```

Enforcement actions (fines, warnings) are not on these registers, so they are
not events yet. See "Not built" below.

## Verdicts: never a bare yes/no

`GET /domains/{domain}` and `registerwatch domain` give one verdict per
jurisdiction. Each has an explanation, caveats, a confidence, and the listings
and blocks behind it (`model/verdict.py`):

| verdict | meaning | confidence |
|---|---|---|
| `conflict` | listed as authorised and on a blocklist at the same time | high |
| `blocked` | on a blocklist now | high |
| `authorised` | listed now, and nothing on the register contradicts it | high, or medium with a caveat |
| `listed_not_operating` | listed, but the listing or its licence is inactive, suspended, revoked, expired…, or its party holds no active licence on that register | high |
| `blocked_parent` | a parent domain is blocked | medium |
| `related_listed` | a different host under the same domain is listed (nj.bet365.com) | low |
| `previously_listed` / `previously_blocked` | was, and was removed (dated) | medium |
| `not_listed` | the register lists websites; this one is not on it | low |
| `not_blocked` | only a blocklist is held here | low |
| `no_domain_data` | the register lists licensees but not websites | none |

Confidence means how directly official register data answers "may this host
offer gambling here?". Caveats carry the grey areas the plan lists in step 16:

- a white-label listing;
- a licence whose end date has passed but which is still listed;
- New Jersey listing sites under the casino licensee that holds the permit;
- not being listed is not proof of illegality;
- not being blocked does not mean authorised.

```
$ registerwatch domain bet365.com                # regulator names shortened
bet365.com: authorised in GB, SE; blocked in CH; look closer at US-NJ
CH     Blocked              high  bet365.com is on the blocklists of ESBK and Gespa, listed 2019-09-03.
GB     Authorised listing   high  bet365.com is listed.
SE     Authorised listing   high  bet365.com is listed for Hillside (Europe) ENC under licence Kommersiellt online.
US-NJ  Related host listed  low   bet365.com is not listed, but nj.bet365.com is.
```

The same answer lists **the same name elsewhere**: other registrable domains
with the label `bet365` (bet365.de listed in DE, bet365.es blocked in CH…). It is
marked as a lead, because the registers never make that link themselves.

## Evidence and data quality

- Every model row has `evidence`: the register table, row id, and the snapshots
  that first saw and removed it. Those rows link to `raw_snapshots`, whose
  manifests hold the downloaded bytes. A claim can be followed back to the file
  the regulator published.
- Every API answer that cites a register carries `sources`: the regulator, the
  register's own page, and when our copy was last read successfully.
- `GET /coverage` / `registerwatch coverage` reports, per register: what it
  covers, its publication cadence where documented, its freshness, and the
  share of current rows with an identifier, a stated status, an end date and a
  parseable hostname. It also lists the 23 jurisdictions checked and found to
  have no usable register (Malta, Curaçao, the Netherlands…; see REGULATORS.md),
  so "no data" is an answer rather than silence.
- `GET /status` turns 503 when the model trails the newest complete snapshot by
  more than an hour, the same way it does for a stale register.

## The plan, step by step

| step | what the plan asks for | here |
|---|---|---|
| 1–5 | competitors, their UI/UX, coverage, pricing, customer problems | **Not built.** These are desk research and need visits to competitors' products. The coverage matrix (`/coverage`) is our own column of step 3's matrix. |
| 6 | master jurisdiction list | 20 covered jurisdictions plus 23 checked-and-uncovered, each with a reason (`catalogue.py`, REGULATORS.md). Unassessed ones are listed in REGULATORS.md. |
| 7 | jurisdiction-by-jurisdiction differences | What each register lets us answer (`/jurisdictions/{code}/profile`): coverage, licence statuses, licensed products, recent changes. Legislation, advertising, KYC and tax rules are **not built**. |
| 8–9 | regulators, official data sources | One register module per source, plus `catalogue.COVERAGE`: scope, cadence, what it publishes. |
| 10 | operators / legal entities | `parties`, with company IDs where published (UKGC account, Czech and Slovak IČO), clustered into operators. |
| 11 | licences | `licences`: type, products, channel, area, site, status (published and normalised, with the date a status took effect), validity, issuing authority. |
| 12 | brands, domains, blocklists | `brands`, `domains` (host, registrable domain, label), `blocks` with listing dates. |
| 13 | rules and regulations | **Not built.** No register publishes rules; this needs legislation sources. |
| 14 | enforcement and regulatory events | Register-derived events: licences added, removed, status-changed; domains listed, delisted; blocks added, lifted. Fines and warnings are **not built**. They need the regulators' enforcement pages as new sources. |
| 15 | history and changes | Every row's first seen, last seen, removed and versions; `/events` with type, jurisdiction, operator and domain filters. |
| 16 | grey areas | Verdicts and caveats above; operator `flags` (licence suspended, end date passed, listed in one jurisdiction and blocked in another). |
| 17 | evidence and data quality | Above. |
| 18 | customer questions | Is this domain licensed / blocked / by whom / since when → `/domains`. Which jurisdictions, licences, brands, domains does this company have → `/operators/{id}`. What changed this week → `/events`. Which licences are suspended → `/licences?status=suspended`. Who regulates this market and what is licensed → `/jurisdictions/{code}/profile`. |
| 19 | website information architecture | `GET /`: lookup by website or company, a verdict card per jurisdiction with the entity → licence → website chain and the evidence beside each claim, operator pages, change feed, coverage. |
| 20 | API information requirements | The endpoints above. The OpenAPI spec is at `/openapi.json`. |
| 21–22 | commercial products, gap analysis | **Not built.** These depend on steps 1–5. |

## Not built, and where to start

- **Enforcement.** UKGC, Spelinspektionen, the KSA and others publish enforcement
  actions as pages, not registers. Each would be a new register module with
  `kind` set to a new value, plus an `enforcement` fact kind feeding events of
  type `enforcement.*`.
- **More jurisdictions.** REGULATORS.md lists candidates. Pennsylvania's PDF can
  follow ESBK's approach.
- **Rules (step 13).** These need a different source type, such as legislation
  and guidance, with an effective date and history per rule. The model's
  `RowRef`/event machinery fits them, but no source is in yet.
- **Entity resolution beyond names.** Operators are clustered on exact legal
  names. Ownership (parent and subsidiary) needs a company-registry source.
  Linking two legal entities, such as Hillside (Europe) ENC and Hillside (New
  Media Malta) PLC, is deliberately left to a human. The domain labels already
  show the link.

## Adding a register to the model

A new register module needs three more things:

1. A projection in `model/projections.py`, added to `PROJECTIONS`.
2. A coverage entry in `model/catalogue.py`.
3. `uv run pytest tests/test_model.py`. A test fails until the projection
   yields exactly the kinds the catalogue says it covers, and its links resolve.

Bump `MODEL_VERSION` in `model/__init__.py` when a projection's output changes
for rows it already handled.
