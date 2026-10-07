# Problem types

Every error from `/v1` is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
problem, sent as `application/problem+json`:

```json
{
  "type": "https://github.com/mishmish004/RegisterWatch/blob/main/docs/problems.md#invalid-parameter",
  "title": "Invalid parameter",
  "status": 400,
  "detail": "limit: Input should be less than or equal to 1000",
  "instance": "/v1/registers/gb_ukgc/tables/licences/rows",
  "request_id": "0199c1a8-7c3e-7a52-9d1e-5b6f0c2a4e11",
  "errors": [{"field": "limit", "location": "query", "message": "Input should be less than or equal to 1000"}]
}
```

- `type` is the stable identifier: branch on it, not on `title` or `detail`. It links to a section below.
- `detail` is for people and may change wording.
- `instance` is the request path. `request_id` is also in the `X-Request-Id` response header and in the server's log line for the request; quote it when reporting a problem.
- `errors` (400 only) names each bad parameter, with `location` one of `query`, `path`, `header`.
- Clients must ignore members they do not know; a type may gain extension members.

The list is generated from `src/registerwatch/http/problems.py` (`Catalog`), and a test
checks that every entry has a section here.

## invalid-parameter

**400.** A parameter is out of range, malformed, repeated where it may appear once, or not a
parameter of the operation at all (`?jurisdictions=gb` on an operation that takes
`jurisdiction`). `errors` names each one. Fix the request; retrying it unchanged gives the
same answer.

## unknown-filter-column

**400.** A `filter[<column>]` names a column the table does not have. `detail` lists the
table's columns; `getTable` returns them too.

## invalid-cursor

**400.** The `cursor` is not one this API issued, or it was issued for a different query
(other filters, other `q`, another table). Start again from the first page with the same
parameters you are paging through.

## unauthenticated

**401.** The deployment requires a token and the request carried none, or the wrong one. Send
`Authorization: Bearer <token>`. The response carries `WWW-Authenticate: Bearer realm="registerwatch"`,
with `error="invalid_token"` added when a token was sent but is not one this route takes.

## forbidden

**403.** The token is valid but cannot do this, such as a read token on an ingest operation.

## not-found

**404.** No operation lives at this path. The operations are listed in `/openapi.json`.

## jurisdiction-not-found

**404.** The jurisdiction code in the path is not one this deployment covers. `detail` lists
the known codes; `listJurisdictions` returns them too.

## register-not-found

**404.** The register slug in the path is unknown. `listRegisters` returns every slug.

## table-not-found

**404.** The register has no table by that name. `detail` lists its tables.

## row-not-found

**404.** No row with that id exists in the table.

## ingest-run-not-found

**404.** No ingest run with that id exists.

## method-not-allowed

**405.** The path exists but does not accept this method. The `Allow` header lists the ones it does.

## ingest-in-progress

**409.** An ingest run is already active; only one runs at a time. Wait for it to finish
(`Retry-After`) and look at the active run before starting another.

## content-too-large

**413.** The request body is over 64 KiB; no operation takes one that large (an ingest run's
body is a few hundred bytes). A body declared that large is refused before any of it is read,
and the server closes the connection after this answer.

## unsupported-media-type

**415.** The request body is not `application/json`.

## idempotency-key-reused

**422.** The `Idempotency-Key` was already used with a different request body. Use a new key
for a new request.

## rate-limited

**429.** Too many requests for this token (or client address, without a token of this
deployment's). Wait `Retry-After` seconds. Every limited response carries `RateLimit-Policy`
(the quota) and `RateLimit` (`r` requests left, more in `t` seconds), so a client can slow down
before it gets here.

## internal

**500.** Something failed on the server. The body says nothing about why, on purpose; quote
`request_id` when reporting it.

## ingest-disabled

**503.** This deployment has no ingest token configured, so ingest operations are switched off.

## database-unavailable

**503.** The database cannot be reached right now, or every connection is busy: the request
waited `DB_POOL_TIMEOUT_S` (3 s) for one. Retry after `Retry-After` seconds. `/readyz` answers
with it when the database does not answer `SELECT 1` within 2 s.

## query-timeout

**504.** The query ran past the server's time limit and was cancelled. Narrow it (a filter, a
longer `q`, a smaller `limit`) and retry.
