# registerwatch

Daily snapshots of gambling regulators' public registers: who is licensed, which
websites they may run, and which domains are blocked. There are 21 registers
across 18 jurisdictions, and each gets its own Postgres schema. Rows are kept
with first-seen and removed markers, so what changed on any day is a query.

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

# as a service (see Container below)
docker build -t registerwatch . && docker run -p 8000:8000 --read-only --tmpfs /tmp --env-file .env registerwatch
```

Then once per database:

```bash
cp .env.example .env                        # DATABASE_URL, USER_AGENT, INGEST_TOKEN
registerwatch migrate                       # public tables + one schema per register
registerwatch ingest                        # every register; the daily job
```

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
registerwatch serve                                  # the API on :8000 ($PORT)
registerwatch migrate | registers | ddl | openapi
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
and committed as `src/registerwatch/migrations/20261007000007_register_schemas.sql`
(mirrored in `supabase/migrations/`), and a test
fails if the two disagree. A schema change is a new file with a new version:
Supabase applies each version once, so older versions stay in
`supabase/migrations/` as history and the package ships only the newest. Each table has the register's own columns plus:

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
The same spec is committed as `openapi/v1.yaml` (`registerwatch openapi --write`;
a test fails when it is stale). `scripts/verify/lint.sh` lints it and
`scripts/verify/breaking.sh` fails on a change that would break clients;
`tests/test_contract.py` holds the app to it with generated requests.
Reads are open unless `READ_TOKEN` is set; operations need `INGEST_TOKEN`. Both are bearer
tokens of at least 32 characters (the spec declares them as `ReadToken` and `IngestToken`).

**v1** — being built under `/v1` ([plan.md](plan.md)); the routes below stay
until it is complete. Same read token. Collections come as
`{"data": [...], "pagination": {"next_cursor", "has_more", "limit", "total"}}`;
pass `next_cursor` back as `cursor` for the next page (also in a `Link: rel="next"` header).

| | |
|---|---|
| `GET /v1/jurisdictions`, `/v1/jurisdictions/{code}` | codes, names, registers; the detail adds each register's freshness |
| `GET /v1/registers`, `/v1/registers/{slug}`, `/v1/registers/{slug}/tables/{table}` | registers, tables, typed columns, freshness |
| `GET /v1/registers/{slug}/tables/{table}/rows` | current rows in `id` order. `filter[status]=Active`, `q`, `limit` (≤1000), `cursor`, `include_total`; a walk reads one snapshot throughout |
| `GET /v1/registers/{slug}/tables/{table}/rows/{id}` | one row, current or not, with its history |
| `GET /v1/registers/{slug}/changes`, `/v1/jurisdictions/{code}/changes` | rows added and removed, oldest first, `since`/`until` (RFC 3339 with offset), never truncated |
| `GET /v1/registers/{slug}/snapshots` | every ingest run, complete or not, newest first, with the reason when it was not |
| `GET /v1/search?q=betway&jurisdiction=gb&jurisdiction=de` | one entry per table with a match, with a `rows_url` for all of them |
| `GET /v1/domains/{domain}` | `licensed_in`, `blocked_in` and each matching row |
| `POST /v1/ingest-runs` | ingest token. Body `{"registers": [...]}`, `{"jurisdiction": "ch"}` or `{}` for all, plus `force`, `accept_count_delta`. 202 with the run and `Location`; 409 linking the active run (one at a time across every replica); `Idempotency-Key` replays the same run for 24 h |
| `GET /v1/ingest-runs`, `/v1/ingest-runs/{id}` | ingest token. Runs newest first with per-register results; stored in Postgres, so they survive restarts. A run cut short by shutdown is `partial` with `not_started` |
| `GET /v1/status` | open. Every register's freshness, `stale`, `stale_registers` and `database` (`ok`, or `unreachable` when it does not answer within 2 s). Always 200, so stale data never looks like a broken server; `?strict=true` answers the same document with **503** when anything is stale, for uptime monitors |
| `GET /livez` | open, unversioned. The process answers; touches nothing. Point a liveness probe here |
| `GET /readyz` | open, unversioned. The database answers `SELECT 1` within 2 s, else 503 `database-unavailable`. Point a readiness probe here |

Errors from `/v1` are RFC 9457 problems (`application/problem+json`); each `type`
links to its section of [docs/problems.md](docs/problems.md). A parameter an
operation does not take is a 400, not ignored. Every response, legacy included,
carries `X-Request-Id` (yours if you send a short one, else a UUIDv7), and the
server's log line for the request has the same id.

Every `/v1` read has a weak `ETag` naming the snapshots it was read at; send it
back as `If-None-Match` and an unchanged answer is a 304 with no body. New data
shows within 30 s of its snapshot. Reads are `Cache-Control: public, max-age=300`
(`private` when `READ_TOKEN` is set) with `Vary: Authorization, Accept-Encoding`;
ingest and status answers are `no-store`. Requests are rate limited per bearer
token, or per client address without one of ours: 600 reads, 60 searches and
domain checks, and 10 ingest starts a minute (`RATE_LIMIT_READ_PER_MIN`,
`RATE_LIMIT_SEARCH_PER_MIN`, `RATE_LIMIT_INGEST_PER_MIN`; 0 turns one off).
Each limited response carries `RateLimit-Policy` and `RateLimit` (`r` left,
more in `t` seconds); over the limit is a 429 with `Retry-After`. The buckets
live in each process, so every replica counts on its own, and behind a proxy
that uvicorn does not trust (`FORWARDED_ALLOW_IPS`) all anonymous clients share
the proxy's address. A v1 statement running past `READ_STATEMENT_TIMEOUT_MS`
(5000) is cancelled: a 504 `query-timeout`.

Reads share a pool of `DB_POOL_MAX` (10) connections; a request that waits
`DB_POOL_TIMEOUT_S` (3) for one is a 503 `database-unavailable` with
`Retry-After: 5`. Ingest runs have 2 connections of their own, so a batch never
takes one a read needs, and the run lock one more: a process opens at most
`DB_POOL_MAX + 3`, each named in `pg_stat_activity.application_name`
(`registerwatch`, `registerwatch-ingest`, `registerwatch-lock`). Request
handlers run in `2 × DB_POOL_MAX` threads, so a burst queues for a thread rather
than timing out on the pool.

**Reading** — open, or `Authorization: Bearer $READ_TOKEN` when set

| | |
|---|---|
| `GET /jurisdictions` | codes, names, registers |
| `GET /jurisdictions/{code}` | registers, tables, columns, last good snapshot |
| `GET /jurisdictions/{code}/{slug}/{table}` | current rows. `?q=` substring in any text column; any other parameter filters a column by exact value, e.g. `/jurisdictions/gb/gb_ukgc/licences?status=Active&type=Remote`; `?limit` (≤1000) `?offset` |
| `GET /jurisdictions/{code}/changes?since=2026-10-01` | rows added and removed since a date, per register and table |
| `GET /search?q=betway&jurisdiction=gb,de` | every table with a match, most matches first |
| `GET /check/domain/{domain}` | `licensed_in`, `blocked_in` and each matching row; `www.` ignored, subdomains reported as `subdomain` |

**Operations** — `Authorization: Bearer $INGEST_TOKEN`

| | |
|---|---|
| `POST /ingest/{slug\|all}` | 202, runs in the background; 409 if a run is going. `?force` `?accept_count_delta` `?wait` |
| `POST /jurisdictions/{code}/ingest` | the same for one jurisdiction's registers |
| `GET /ingest/last` | the last batch this process finished |

**Health** — open

| | |
|---|---|
| `GET /health` | liveness; touches nothing |
| `GET /status` | per register: last attempt, last good snapshot, last verdict. **503** if any register has no good snapshot within `STALE_AFTER_H` (26h), or the database is unreachable. Replaced by `/v1/status?strict=true` for uptime monitors and `/readyz` for probes |
| `GET /registers` | every register with its schema, tables and columns |

## Container

The `Dockerfile` builds the virtualenv with uv from `uv.lock` in one stage and
copies only that into `python:3.12-slim-bookworm` (about 240 MB unpacked). It runs
as `registerwatch` (uid 10001) and needs no writable disk but `/tmp`, so
`--read-only --tmpfs /tmp` works; with `BLOB_BACKEND=local`, point `SNAPSHOT_ROOT`
under `/tmp`. The server is PID 1 and takes SIGTERM itself. Its `HEALTHCHECK` asks
`/livez` every 10 s (every second while it starts); orchestrators should probe
`/livez` for liveness and `/readyz` for readiness.

`registerwatch serve` takes these, flag or environment variable:

| | |
|---|---|
| `--workers`, `WEB_CONCURRENCY` (1) | processes. Each has its own pools (`DB_POOL_MAX + 3` connections) and rate-limit buckets |
| `--timeout-keep-alive` (75 s) | longer than the 60 s idle timeout most proxies use, so the proxy closes an idle connection first |
| `--limit-concurrency` (200) | beyond this many open connections or requests, a new request gets uvicorn's own 503 (`text/plain`, not a problem document) at once. Idle keep-alive connections count |
| `--forwarded-allow-ips`, `FORWARDED_ALLOW_IPS` (127.0.0.1) | proxies whose `X-Forwarded-For`/`-Proto` are believed. Set it to the platform proxy's addresses, or `*` if nothing else can reach the app; until then every anonymous client shares the proxy's rate-limit bucket |
| `SHUTDOWN_GRACE_S` (60) | after SIGTERM, how long a running ingest may take to finish its register |
| `--timeout-graceful-shutdown` (`SHUTDOWN_GRACE_S` + 5) | how long the server waits for that and for requests in flight |

On SIGTERM the server stops accepting connections, requests in flight finish,
and a running ingest finishes the register in hand and records the rest as
`not_started` (`partial`). The process exits 0, or 1 when something was still
running 4 s after the server stopped waiting: then the run stays `running`, and
the next process to take the run lock marks it `failed` (`worker lost`). Give the
platform's stop timeout (Docker's is 10 s, Kubernetes' 30 s) more than
`SHUTDOWN_GRACE_S + 10`, or lower `SHUTDOWN_GRACE_S` to fit it.
`scripts/verify/os.sh` checks all of this against a built image.

## On the network

TLS, the certificate, the redirect from http and HTTP/2 are the platform proxy's:
Railway, Fly and Render all terminate TLS in front of the container. The app has
to know which proxy to believe, so set `FORWARDED_ALLOW_IPS` to the addresses it
connects from (or `*` when nothing else can reach the container). Until then
every anonymous client shares the proxy's rate-limit bucket, and no answer
carries `Strict-Transport-Security`, which goes only on requests that came in
over https.

| | |
|---|---|
| security headers | every answer has `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`. `/docs` and `/redoc` get a policy that lets Swagger UI and Redoc load from jsDelivr, and nothing else |
| `Strict-Transport-Security: max-age=31536000` | on answers to https requests (`X-Forwarded-Proto` from a trusted proxy) |
| gzip | bodies of 1 KiB and more, for clients that send `Accept-Encoding: gzip`: a 1000-row page goes from 325 KB to 29 KB |
| slow clients | a request has 10 s from its first byte to arrive whole, else a 408 and the connection closes. A connection that sends nothing is closed after the keep-alive (75 s) |
| size limits | a request head over 64 KiB is a 431. A body over 64 KiB is a 413 `content-too-large`, answered before the body is read when its length is declared |
| CORS | off: the API is for servers. `CORS_ALLOW_ORIGINS` (comma-separated, `scheme://host[:port]`) lets pages from those origins read with `GET`, without cookies |

Sizing: a worker is one process on one CPU. One worker serves the load
`net.sh` uses (mostly 100-row pages, some domain checks and searches) up to
about 80 requests a second (p95 62 ms); at 100 its answers queue for seconds.
`WEB_CONCURRENCY=2` on two CPUs serves 100 a second at p95 70 ms, and 50
1000-row pages a second at p95 59 ms. Each worker keeps its own rate-limit
buckets, so with two a client may get up to twice its quota.

`scripts/verify/net.sh` checks all of this against a built image, directly and
through a TLS proxy: the deployment's (`RW_EDGE=https://your-host`), or without
one a local Caddy standing in for it.

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
