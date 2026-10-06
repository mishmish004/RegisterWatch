# registerwatch

Daily snapshots of gambling regulators' public registers: who is licensed, which
websites they may run, and which domains are blocked. There are 21 registers
across 18 jurisdictions, and each gets its own Postgres schema. Rows are kept
with first-seen and removed markers, so what changed on any day is a query.

On top of the registers sits **the model** ([docs/MODEL.md](docs/MODEL.md)). It
joins all registers into parties, licences, brands, websites and blocklist
entries, with a change feed and a per-jurisdiction answer for any website that
is never a bare yes/no. It is the product described in the R&D plan
([docs/research-plan.md](docs/research-plan.md)), built from the data so far.

[REGULATORS.md](REGULATORS.md) lists what is scraped. It also lists every
regulator that was checked and is not scraped, and why.

## Install

It is a normal Python package (`pyproject.toml`, built by uv), with one console
command, `registerwatch`, and the API inside it. The migrations and the
certificate it needs ship in the wheel, so an installed copy works from any
directory; configuration comes from environment variables, or a `.env` in the
working directory.

```bash
# from this checkout, for development
uv sync                                     # .venv from uv.lock, dev tools included

# as a package
uv build                                    # dist/registerwatch-0.2.0-py3-none-any.whl (+ sdist)
uv tool install dist/registerwatch-0.2.0-py3-none-any.whl   # `registerwatch` on your PATH
pipx install dist/registerwatch-0.2.0-py3-none-any.whl      # same, with pipx
pip install dist/registerwatch-0.2.0-py3-none-any.whl       # into an existing environment

# as a service
docker build -t registerwatch . && docker run -p 8000:8000 --env-file .env registerwatch
```

Then once per database:

```bash
cp .env.example .env                        # DATABASE_URL, USER_AGENT, INGEST_TOKEN
registerwatch migrate                       # public tables, the model schema, one schema per register
registerwatch ingest                        # every register, then rebuilds the model; the daily job
```

Upgrading a database that already holds snapshots: run `registerwatch migrate`
then `registerwatch build`. Until a build has run, `/status` reports the model
as behind the registers.

## CLI

Every jurisdiction is a command group: `gb`, `de`, `ch`, `us-nj`, `ca-on`… — run
`registerwatch jurisdictions` for the list. Output is a table by default, or
`-f csv` / `-f json` for piping.

```bash
registerwatch jurisdictions                          # codes, names, registers
registerwatch gb                                     # GB: registers, tables, columns
registerwatch gb rows licences -w status=Revoked     # current rows; -w COLUMN=VALUE repeatable
registerwatch gb rows domain_names -q bet365 -f csv  # substring in any text column
registerwatch gb search betway                       # every GB table
registerwatch gb changes --since 2026-10-01          # rows added / removed
registerwatch gb ingest [--force]                    # fetch GB now
registerwatch gb status                              # freshness; exit 1 if stale
registerwatch ch rows ch_esbk.blocked_domains        # two registers share a table name: qualify it

registerwatch search betway -j gb,de,se              # across jurisdictions
registerwatch check-domain bet365.com                # licensed where, blocked where
registerwatch ingest gb de pl_mf                     # codes and slugs mix; default all
registerwatch status [-j ch]                         # exit 1 if any register is stale
registerwatch serve                                  # the API and lookup page on :8000 ($PORT)
registerwatch migrate | registers | ddl
```

The model answers across every register at once, with the evidence:

```bash
registerwatch domain bet365.com                      # a verdict per jurisdiction (--all for every one)
registerwatch operators hillside                     # companies by name, trading name or website
registerwatch operator hillside-europe-enc           # footprint, licences, websites, flags, history
registerwatch licences -j gb,se --status suspended   # --product casino, -q, --all for removed ones
registerwatch events --since 2026-10-01 -t licence.status_changed,block.added
registerwatch gb profile                             # coverage, licence statuses, products, recent changes
registerwatch coverage                               # per register: covers, freshness, data quality
registerwatch build                                  # rebuild the model now (ingest does it itself)
```

```
$ registerwatch domain bet365.com
bet365.com: authorised in GB, SE; blocked in CH; look closer at US-NJ
```

For example, against the production data on 5 Oct 2026:

```
$ registerwatch check-domain bet365.com
bet365.com: licensed in GB, SE, US-NJ; blocked in CH, IT
```

Every register is fetched, stored, parsed, checked and recorded by one engine
(`src/registerwatch/ingest/engine.py`), whatever the register's format:

1. **Resolve parts.** These are fixed URLs, or a plan that reads an index page
   to find the newest file (ADM's TXT, Gespa's dated release, the Czech
   ministry's dated page, the Isle of Man's dated XLSX). An expected file the
   index no longer links is recorded as `NOT_LISTED`, so it is never silently
   dropped.
2. **Download** each part independently. Every request goes through
   `fetch/http.py`, which handles robots.txt, rate limits per host,
   retries and the evidence log. A 304 reuses the previous run's file after
   checking its digest.
3. **Store** the raw bytes before anything judges them. Blobs go to
   `<root>/<slug>/<date>/<run_id>/` with a `manifest.json`.
4. **Parse and check.** Each table's parser turns the bytes into typed rows.
   The rows must then meet the table's declared shape:
   - a minimum number of rows;
   - required columns filled in;
   - a row count within tolerance of the last good run;
   - known values, or a warning naming the new ones.
5. **Record** one `raw_snapshots` row per register per run, complete or not.
   Only a complete run writes rows to the register's schema.

Step 5 is the important rule. A truncated file, a page whose layout moved, or
an uptime-check JSON served in place of the page (ACMA does this) is recorded
as an incomplete snapshot with a precise reason, for example
`PAGE_GAP:3/4 licences:HTTP_503`, `INVALID licences:HEADER_MISMATCH` or
`INVALID providers:COUNT_DELTA:219->12`. None of it can remove a single
licence from the register's tables, so a broken run can never look like a
mass revocation.

A row count that moves more than the tolerance is held, because a truncated
export and a mass revocation look identical. After checking it, run
`ingest <slug> --accept-count-delta` (or `?accept_count_delta=true`).

## Schemas

`registerwatch ddl` prints the SQL. It is generated from the register modules
and committed as `src/registerwatch/migrations/20261004000005_register_schemas.sql`
(mirrored in `supabase/migrations/`), and a test
fails if the two disagree. Each table has the register's own columns plus:

| column | meaning |
|---|---|
| `row_hash` | the row's identity: sha256 of its values, plus an occurrence number so exact duplicates stay distinct |
| `first_seen_snapshot_id` | first complete snapshot containing the row |
| `last_seen_snapshot_id` | newest complete snapshot still containing it |
| `removed_snapshot_id` | first complete snapshot without it; NULL while current |

`<schema>.current_<table>` views show each register as it stands. What changed
in snapshot *x* is `WHERE first_seen_snapshot_id = x OR removed_snapshot_id = x`.

## API

`registerwatch serve` (or the Docker image) runs it; interactive docs at
`/docs`, the OpenAPI spec at `/openapi.json` — generate a client from that.
Reads are open unless `READ_TOKEN` is set; operations need `INGEST_TOKEN`.

**Reading** — open, or `Authorization: Bearer $READ_TOKEN` when set

| | |
|---|---|
| `GET /jurisdictions` | codes, names, registers |
| `GET /jurisdictions/{code}` | registers, tables, columns, last good snapshot |
| `GET /jurisdictions/{code}/{slug}/{table}` | current rows. `?q=` substring in any text column; any other parameter filters a column by exact value, e.g. `/jurisdictions/gb/gb_ukgc/licences?status=Active&type=Remote`; `?limit` (≤1000) `?offset` |
| `GET /jurisdictions/{code}/changes?since=2026-10-01` | rows added and removed since a date, per register and table |
| `GET /search?q=betway&jurisdiction=gb,de` | every table with a match, most matches first |
| `GET /check/domain/{domain}` | `licensed_in`, `blocked_in` and each matching row; `www.` ignored, subdomains reported as `subdomain` |

**The model** (same authentication). See [docs/MODEL.md](docs/MODEL.md). `GET /` serves a lookup page over these.

| | |
|---|---|
| `GET /domains/{domain}` | a verdict per jurisdiction: `authorised`, `blocked`, `listed_not_operating`, `related_listed`, `previously_listed`, `not_listed`, `no_domain_data`… each with an explanation, caveats, confidence and the register rows behind it; plus the same name elsewhere and the host's history. A URL works too |
| `GET /operators?q=hillside` | operators matching a company name, trading name or website |
| `GET /operators/{operator_id}` | one operator across registers: footprint, licences, brands, websites, its websites on blocklists, flags, related operators, history |
| `GET /licences?jurisdiction=gb&status=suspended&product=casino` | licences; `q` searches reference, type and holder; `current=false` for removed ones |
| `GET /events?since=2026-10-01&type=licence.status_changed,block` | the change feed; `jurisdiction`, `operator`, `domain` filter it; defaults to the last 7 days |
| `GET /jurisdictions/{code}/profile` | what the registers cover, counts, licensed products, recent changes |
| `GET /coverage` | per register: coverage, cadence, freshness, data quality; the jurisdictions with no usable register |

**Operations** — `Authorization: Bearer $INGEST_TOKEN`

| | |
|---|---|
| `POST /ingest/{slug\|all}` | 202, runs in the background; 409 if a run is going. `?force` `?accept_count_delta` `?wait` |
| `POST /jurisdictions/{code}/ingest` | the same for one jurisdiction's registers |
| `POST /build` | rebuild the model now; 409 while an ingest runs. An ingest batch that recorded a complete snapshot rebuilds it anyway |
| `GET /ingest/last` | the last batch this process finished |

**Health** — open

| | |
|---|---|
| `GET /health` | liveness; touches nothing |
| `GET /status` | per register: last attempt, last good snapshot, last verdict, and the model's last build. **503** if any register has no good snapshot within `STALE_AFTER_H` (26h), if the model trails the newest complete snapshot by more than an hour, or if the database is unreachable — point an uptime monitor at it |
| `GET /registers` | every register with its schema, tables and columns |

## Daily schedule (Supabase)

Supabase cannot run Python, so the schedule lives in Supabase and the work in
the API. `supabase/migrations/20261004000004_schedule_daily_ingest.sql` uses
pg_cron and pg_net to POST `/ingest/all` at **06:00, 08:00 and 10:00 UTC**.
Registers that already have a good snapshot skip the later slots, so those
slots retry only what failed.

1. Deploy the API somewhere Supabase can reach over HTTPS. The `Dockerfile`
   runs on Railway, Fly or Render. Set `DATABASE_URL`, `INGEST_TOKEN` and
   `USER_AGENT` (a real contact), plus `BLOB_BACKEND=s3` and the `S3_*` keys,
   because a container's disk does not survive a deploy.
2. In the Supabase SQL editor, once:
   ```sql
   select vault.create_secret('https://<your-api-host>', 'registerwatch_api_url');
   select vault.create_secret('<same INGEST_TOKEN>', 'registerwatch_ingest_token');
   ```
3. Run `supabase db push`, or paste the migrations into the SQL editor.
4. Check it without waiting for 06:00:
   ```sql
   select registerwatch_private.trigger_ingest();          -- all; or trigger_ingest('pl_mf')
   select status_code, content from net._http_response order by created desc limit 1;  -- 202
   select * from cron.job_run_details order by start_time desc limit 5;
   ```

## Testing against Postgres

```bash
docker run -d --rm --name rw-pg -e POSTGRES_PASSWORD=rw -e POSTGRES_DB=rw -p 55432:5432 postgres:16-alpine
REGISTERWATCH_TEST_DATABASE_URL=postgresql://postgres:rw@localhost:55432/rw uv run pytest tests/test_postgres.py
```

This applies every migration and loads every register's real fixture rows
through COPY. It also runs the engine three times against real SQL, checking
that a change is one row out and one row in, and that a failed run changes
nothing.

## Things the data does that the code must not "fix"

- **UKGC licence numbers are not stable.** The last segment is a version
  counter: about 480 of about 4,500 rows moved in one week of August 2026.
  The history records these as one row out and one row in. A differ must pair
  them on account + licence number without its last segment + activity.
- The **Isle of Man's** page and XLSX spell four companies differently, for
  example "RedPlay Limited" and "Redplay Limited". Both are kept as published.
- **ACMA** answers user agents containing "compliance change monitoring" with
  its 59-byte uptime-check JSON most of the time. The default user agent avoids
  "monitoring"; a wrong kind of answer is retried once in the same run and,
  if it persists, recorded as `CONTENT_TYPE:application/json`.
- **Poland's** API builds a ~9 MB XML per request; the register sets a 180 s
  timeout (`timeout_s`) instead of the default 30 s.
- **www.urhh.sk** (Slovakia) does not send its intermediate certificate.
  `registers/certs/disig_r2i2.pem` completes the chain with verification still
  on.
- **Domain columns** are free text in several registers: schemes, paths, an
  encoded zero-width space, one URL repeated four times. The published text
  is kept, and `host` holds the parsed hostname or NULL.
- **Ireland's** Revenue registers name individuals (relevant officers,
  sole-trader bookmakers). They are public registers; handle the copies with
  matching care.

## Adding a register

Write a module under `src/registerwatch/registers/`. Copy the closest one:
- `gr_hgc` for a fixed file;
- `it_adm` for a file discovered from a page;
- `es_dgoj` for a paginated site.

Then:
1. Add one line in `registers/__init__.py`.
2. Add gzipped fixtures under `tests/fixtures/registers/<slug>/`.
3. Run `uv run registerwatch ddl --write`.
4. Write its projection in `src/registerwatch/model/projections.py` and its
   coverage in `src/registerwatch/model/catalogue.py`; `tests/test_model.py`
   fails until the two agree.
