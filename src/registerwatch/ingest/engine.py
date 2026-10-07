"""One register, one run: discover -> download -> store -> parse -> check -> record.

  1. resolve parts   fixed URLs, or a plan that reads index pages first; every
                     discovery request goes through fetch.http and is logged
  2. download        each part independently: one 503 does not discard the rest.
                     A 304 reuses the previous run's blob after checking its
                     digest; with no usable blob it refetches unconditionally
  3. store           raw bytes before anything judges them — they are evidence
  4. parse + check   each table's parser runs on the stored bytes; the rows are
                     then held to the table's declared shape: minimum rows,
                     required columns, row count against the last good run
  5. record          one raw_snapshots row per run, complete or not, and — only
                     for a complete run — the rows into the register's schema

Step 5 happens even when 1-4 failed or raised. A missing row is a gap in the
evidence, and a gap reads as "no changes".

Only a complete run touches the register's tables. That is the whole defence
against the false revocation: a truncated file or a moved column is recorded as
an incomplete snapshot, and the rows it is missing stay current until a run that
actually saw the whole register says otherwise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from registerwatch import canon
from registerwatch.config import PROJECT_ROOT, settings
# Ingest's own pool (plan.md P8.2): a batch never takes a connection a read needs.
from registerwatch.db.engine import ingest_tx as tx
from registerwatch.db.repos import observations
from registerwatch.db.repos import snapshots as repo
from registerwatch.fetch import limiter
from registerwatch.fetch.http import Fetched, Request, fetch_one
from registerwatch.fetch.limiter import RateBudget
from registerwatch.ingest import validate
from registerwatch.registers.base import Bundle, DiscoveryError, Part, ParseError, Register, Row, Table
from registerwatch.storage.blobs import BlobStore, run_prefix, write_manifest

log = logging.getLogger(__name__)

OK_STATUS = (200, 304)
MIME = {
    "csv": "text/csv", "html": "text/html", "json": "application/json",
    "xml": "application/xml", "txt": "text/plain", "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
CERTS = PROJECT_ROOT / "src" / "registerwatch" / "registers" / "certs"


@dataclass
class IngestResult:
    slug: str
    snapshot_id: int | None
    complete: bool
    reason: str | None
    parts_ok: int
    parts_expected: int
    record_count: int | None = None
    blob_ref: str | None = None
    raw_hash: bytes | None = None
    canonical_hash: bytes | None = None
    unchanged: bool = False
    skipped: bool = False
    tables: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("raw_hash", "canonical_hash"):
            d[k] = d[k].hex() if d[k] else None
        return d


def ingest(register: Register, store: BlobStore, *, force: bool = False,
           accept_count_delta: bool = False) -> IngestResult:
    run_started_at = datetime.now(timezone.utc)
    run_id = uuid.uuid4().hex[:12]

    with tx() as conn:
        src = repo.upsert_source(conn, register.slug, register.name, register.country, register.config())
        source_id = src["id"]
        if not force and repo.fetched_recently(conn, source_id, settings().min_refetch_interval_h):
            log.info("%s: skip, good snapshot within %sh", register.slug, settings().min_refetch_interval_h)
            return IngestResult(register.slug, None, False, "SKIPPED_RECENT", 0, 0, skipped=True)
        validators = repo.etag_map(conn, source_id)
        history = repo.recent_snapshots(conn, source_id)

    previous = history[0] if history else None
    prefix = run_prefix(register.slug, run_started_at, run_id)
    run = _Run(register, store, prefix, validators, _PriorRuns(store, history), accept_count_delta)

    crash: str | None = None
    discovery_failed: str | None = None
    parts: list[Part] = []
    try:
        with httpx.Client(timeout=register.timeout_s or settings().http_timeout_s, follow_redirects=True,
                          verify=_verify(register)) as client:
            run.client = client
            try:
                parts = register.resolve_parts(run.discover)
            except DiscoveryError as exc:
                discovery_failed = f"DISCOVERY_FAILED:{exc.status} {exc.url}"[:500]
                log.error("%s: %s", register.slug, exc)
            except ParseError as exc:
                discovery_failed = f"DISCOVERY_FAILED:{exc}"[:500]
                log.error("%s: discovery page unreadable: %s", register.slug, exc)
            for ordinal, part in enumerate(parts, start=1):
                run.part(part, ordinal)
        if not discovery_failed and run.parts_complete(parts):
            run.parse_tables()
    except Exception as exc:  # noqa: BLE001 — anything at all still gets its row
        log.exception("%s: ingest crashed", register.slug)
        crash = f"EXCEPTION:{type(exc).__name__}:{exc}"[:500]

    expected = [p for p in parts if p.expected]
    parts_ok = sum(1 for p in expected if p.name in run.good_parts)
    tables_ok = bool(run.tables) and all(not t["problems"] for t in run.tables.values())
    complete = (crash is None and discovery_failed is None and bool(expected)
                and parts_ok == len(expected) and tables_ok)

    reason: str | None = None
    if crash:
        reason = crash
    elif discovery_failed:
        reason = discovery_failed
    elif not expected:
        reason = "NO_PARTS"
    elif parts_ok < len(expected):
        detail = " ".join(f"{p.name}:{','.join(run.part_problems.get(p.name, ['?']))}"
                          for p in expected if p.name not in run.good_parts)
        reason = f"PAGE_GAP:{parts_ok}/{len(expected)} {detail}"[:500]
    elif not tables_ok:
        detail = " ".join(f"{name}:{','.join(t['problems'])}"
                          for name, t in run.tables.items() if t["problems"])
        reason = f"INVALID {detail}"[:500]

    raw_hash = canon.combine_hashes(run.raw_digests)
    canonical_hash = run.canonical_hash() if complete else None
    record_count = sum(len(rows) for rows in run.rows.values()) if run.rows else None

    manifest = {
        "source": register.slug,
        "register": register.name,
        "country": register.country,
        "run_id": run_id,
        "run_started_at": run_started_at.isoformat(),
        "parts_expected": len(expected),
        "parts_ok": parts_ok,
        "raw_hash": raw_hash.hex(),
        "canonical_hash": canonical_hash.hex() if canonical_hash else None,
        "canon_version": canon.CANON_VERSION,
        "complete": complete,
        "incomplete_reason": reason,
        "http_status": run.worst_status,
        "validation": {"count_delta_tolerance": register.count_delta_tolerance,
                       "accept_count_delta": accept_count_delta},
        "discovery": run.discovery,
        "parts": run.parts,
        "tables": run.tables,
        "user_agent": settings().user_agent,
    }
    blob_ref = f"{prefix}/manifest.json"
    try:
        blob_ref = write_manifest(store, prefix, manifest)
    except Exception as exc:  # noqa: BLE001 — the row matters more than the manifest
        log.exception("%s: manifest write failed", register.slug)
        complete, canonical_hash = False, None
        reason = f"{reason + ' ' if reason else ''}MANIFEST_UNWRITTEN:{type(exc).__name__}"[:500]

    with tx() as conn:
        snapshot_id = repo.insert_snapshot(
            conn,
            source_id=source_id,
            run_started_at=run_started_at,
            raw_hash=raw_hash,
            blob_ref=blob_ref,
            http_status=run.worst_status,
            pages_expected=len(expected),
            pages_ok=parts_ok,
            fetch_log=run.fetch_log,
            complete=complete,
            incomplete_reason=None if complete else (reason or "INCOMPLETE"),
            record_count=record_count if complete else None,
            canonical_hash=canonical_hash,
            parsed=bool(run.rows),
        )
        if complete:
            # Same transaction as the snapshot row: the history can never
            # describe a snapshot the evidence table does not have.
            observations.apply(conn, register, snapshot_id, run.hashed_rows())

    for host in run.hosts:
        try:
            limiter.relax(host)
        except Exception:  # noqa: BLE001 — bookkeeping must not fail a recorded run
            log.warning("could not relax limiter for %s", host, exc_info=True)

    unchanged = bool(complete and previous and previous.get("canonical_hash") == canonical_hash)
    log.info("%s snapshot=%s parts=%s/%s rows=%s %s%s", register.slug, snapshot_id, parts_ok,
             len(expected), record_count, "complete" if complete else f"INCOMPLETE {reason}",
             " UNCHANGED" if unchanged else "")
    return IngestResult(
        register.slug, snapshot_id, complete, reason, parts_ok, len(expected),
        record_count=record_count if complete else None, blob_ref=blob_ref, raw_hash=raw_hash,
        canonical_hash=canonical_hash, unchanged=unchanged,
        tables={name: len(rows) for name, rows in run.rows.items()},
    )


def _transient(problems: list[str]) -> bool:
    """Wrong *kind* of answer — worth one retry. Anything about the content of
    a well-formed answer (header moved, rows missing) is not: asking again
    would only return the same file."""
    return all(p in ("EMPTY", "HTML_BODY") or p.startswith("CONTENT_TYPE:") for p in problems)


def _verify(register: Register) -> ssl.SSLContext | bool:
    if not register.extra_ca:
        return True
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(CERTS / register.extra_ca))
    return ctx


class _Run:
    """What one run has gathered so far. Exists so a crash halfway through
    still leaves every finished part on hand for the row and the manifest."""

    def __init__(self, register: Register, store: BlobStore, prefix: str,
                 validators: dict[str, dict[str, str]], prior: _PriorRuns,
                 accept_count_delta: bool) -> None:
        self.register = register
        self.store = store
        self.prefix = prefix
        self.validators = validators
        self.prior = prior
        self.accept_count_delta = accept_count_delta
        self.budget = RateBudget(min_interval_ms=register.min_interval_ms)
        self.client: httpx.Client | None = None
        self.fetch_log: list[dict[str, Any]] = []
        self.discovery: list[dict[str, Any]] = []
        self.parts: list[dict[str, Any]] = []
        self.part_problems: dict[str, list[str]] = {}
        self.good_parts: dict[str, bytes] = {}
        self.raw_digests: list[bytes] = []
        self.rows: dict[str, list[Row]] = {}
        self.tables: dict[str, dict[str, Any]] = {}
        self.worst_status = 200
        self.hosts: set[str] = set()
        self._cache: dict[str, Fetched] = {}

    # -- fetching ---------------------------------------------------------------

    def _fetch(self, req: Request, validators: dict[str, str] | None) -> Fetched:
        assert self.client is not None
        fetched = fetch_one(self.client, req, self.budget, validators)
        self.fetch_log.extend(fetched.log)
        self.hosts.add(httpx.URL(req.url).host)
        if fetched.status_code not in OK_STATUS:
            self.worst_status = fetched.status_code
        return fetched

    def discover(self, url: str) -> bytes:
        """Fetch an index page for the register's plan. Logged and stored like
        any part, because "the page listed three files" is the explanation for
        a short snapshot. Cached, so a plan that reads page 0 to find the page
        count does not make the run fetch page 0 twice."""
        if url in self._cache and self._cache[url].body is not None:
            return self._cache[url].body
        n = len(self.discovery) + 1
        fetched = self._fetch(Request(url=url, ordinal=0, label=f"discovery-{n}"), None)
        entry: dict[str, Any] = {"url": url, "status": fetched.status_code}
        if fetched.body is not None:
            key = f"{self.prefix}/_discovery-{n:02d}.html"
            self.store.put(key, fetched.body, "text/html")
            entry.update(key=key, sha256=canon.sha256(fetched.body).hex(), bytes=len(fetched.body))
            self._cache[url] = fetched
        self.discovery.append(entry)
        if fetched.status_code != 200 or fetched.body is None:
            raise DiscoveryError(url, fetched.status_code)
        return fetched.body

    def part(self, part: Part, ordinal: int) -> None:
        base = {"part": part.name, "url": part.url, "ordinal": ordinal,
                "kind": part.kind, "expected": part.expected}
        if part.url is None:
            self._fail(part, base, "NOT_LISTED")
            return

        cached = self._cache.get(part.url)
        req = Request(url=part.url, ordinal=ordinal, label=part.name)
        fetched = cached or self._fetch(req, self.validators.get(part.url))

        raw = fetched.body
        carried: dict[str, Any] | None = None
        if fetched.status_code == 304:
            hit = self.prior.carry(part.name)
            if hit is None:
                # "What you already have is current" — and we do not have it.
                log.warning("%s/%s: 304 with nothing to carry; refetching", self.register.slug, part.name)
                fetched = self._fetch(req, None)
                raw = fetched.body
            else:
                carried, raw = hit
        base["status"] = fetched.status_code

        if raw is None:
            problem = (f"HTTP_{fetched.status_code}" if fetched.status_code not in OK_STATUS
                       else "NOT_MODIFIED_WITHOUT_PRIOR")
            if fetched.log and fetched.log[-1].get("error_class"):
                problem += f"({fetched.log[-1]['error_class']})"
            self._fail(part, base, problem)
            return

        digest = canon.sha256(raw)
        self.raw_digests.append(digest)
        if carried is not None:
            key, content_type = carried["key"], carried.get("content_type")
        else:
            key = f"{self.prefix}/{part.name}.{part.kind}"
            self.store.put(key, raw, MIME[part.kind])
            content_type = fetched.headers.get("content-type")

        problems = validate.check_part(raw, part.kind, content_type)
        rejected: dict[str, Any] | None = None
        if problems and carried is None and part.url not in self._cache and _transient(problems):
            # A server answering the register URL with something else — ACMA's
            # uptime JSON, a maintenance page, nothing — is usually a bad backend,
            # not a changed register (ACMA: 2 of 7 identical requests, 4 Oct 2026).
            # Ask once more now rather than wait for the next slot. The rejected
            # bytes stay stored under the first key and are named in the manifest.
            rejected = {"key": key, "sha256": digest.hex(), "bytes": len(raw),
                        "content_type": content_type, "problems": problems}
            log.warning("%s/%s: %s; retrying once", self.register.slug, part.name, problems)
            self.raw_digests.pop()
            fetched = self._fetch(req, None)
            raw = fetched.body
            base["status"] = fetched.status_code
            if raw is None:
                self._fail(part, {**base, "rejected": rejected}, f"HTTP_{fetched.status_code}")
                return
            digest = canon.sha256(raw)
            self.raw_digests.append(digest)
            content_type = fetched.headers.get("content-type")
            key = f"{self.prefix}/{part.name}.retry-1.{part.kind}"
            self.store.put(key, raw, MIME[part.kind])
            problems = validate.check_part(raw, part.kind, content_type)
        entry = {
            **base, "key": key, "bytes": len(raw), "sha256": digest.hex(),
            "content_type": content_type, "problems": problems, "rejected": rejected,
            "etag": fetched.headers.get("etag") or (carried or {}).get("etag"),
            "last_modified": fetched.headers.get("last-modified") or (carried or {}).get("last_modified"),
            "not_modified": carried is not None,
            "carried_from": self.prior.latest_blob_ref if carried is not None else None,
        }
        self.parts.append(entry)
        if problems:
            self.part_problems[part.name] = problems
            for p in problems:
                log.error("%s/%s rejected: %s", self.register.slug, part.name, p)
        else:
            self.good_parts[part.name] = raw

    def _fail(self, part: Part, base: dict[str, Any], problem: str) -> None:
        log.warning("%s/%s failed: %s", self.register.slug, part.name, problem)
        self.part_problems[part.name] = [problem]
        self.parts.append({**base, "key": None, "problems": [problem]})

    def parts_complete(self, parts: list[Part]) -> bool:
        expected = [p for p in parts if p.expected]
        return bool(expected) and all(p.name in self.good_parts for p in expected)

    # -- parsing ----------------------------------------------------------------

    def parse_tables(self) -> None:
        bundle = Bundle(self.good_parts)
        for table in self.register.tables:
            baseline = self.prior.baseline_rows(table.name)
            try:
                rows = _shape(table, table.parse(bundle))
            except ParseError as exc:
                self.tables[table.name] = {"rows": None, "baseline_rows": baseline,
                                           "problems": [str(exc)[:300]], "warnings": []}
                log.error("%s/%s: %s", self.register.slug, table.name, exc)
                continue
            except Exception as exc:  # noqa: BLE001 — a parser bug is a fact about this run
                log.exception("%s/%s: parser raised", self.register.slug, table.name)
                self.tables[table.name] = {"rows": None, "baseline_rows": baseline,
                                           "problems": [f"PARSER_BUG:{type(exc).__name__}:{exc}"[:300]],
                                           "warnings": []}
                continue
            check = validate.check_table(table, rows, baseline=baseline,
                                         tolerance=self.register.count_delta_tolerance,
                                         accept_count_delta=self.accept_count_delta)
            self.rows[table.name] = rows
            self.tables[table.name] = {
                "rows": len(rows),
                # The count the next run compares against: this one if it
                # passed, otherwise the last one that did — so a held drop stays
                # held until someone accepts it, instead of becoming the normal.
                "baseline_rows": len(rows) if not check.problems else baseline,
                "problems": check.problems,
                "warnings": check.warnings,
            }
            for p in check.problems:
                log.error("%s/%s: %s", self.register.slug, table.name, p)
            for w in check.warnings:
                log.warning("%s/%s: %s", self.register.slug, table.name, w)

    def hashed_rows(self) -> dict[str, list[tuple[bytes, Row]]]:
        return {name: row_hashes(rows) for name, rows in self.rows.items()}

    def canonical_hash(self) -> bytes:
        """The hash over the observation set: every table's row hashes, sorted.
        Equal across two days exactly when the register said the same things,
        whatever happened to its bytes, encoding or row order."""
        h = hashlib.sha256()
        for name, pairs in sorted(self.hashed_rows().items()):
            h.update(name.encode() + b"\n")
            for digest in sorted(d for d, _ in pairs):
                h.update(digest)
        return h.digest()


def _shape(table: Table, rows: list[Row]) -> list[Row]:
    """Hold parser output to the declared columns, and coerce types."""
    names = table.column_names
    out = []
    for i, r in enumerate(rows):
        extra = set(r) - set(names)
        if extra:
            raise ParseError("UNDECLARED_COLUMNS", f"row {i}: {sorted(extra)}")
        out.append({c.name: _coerce(r.get(c.name), c.type) for c in table.columns})
    return out


def _coerce(value: Any, typ: str) -> Any:
    from registerwatch.registers.extract import clean, to_date

    if value is None:
        return None
    if typ == "date":
        return to_date(value)
    if typ == "timestamptz":
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            v = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if typ in ("int", "bigint"):
        try:
            return int(str(value).strip())
        except ValueError:
            return None
    if typ == "numeric":
        try:
            return Decimal(str(value).strip())
        except Exception:  # noqa: BLE001
            return None
    if typ == "bool":
        return bool(value)
    if typ == "text[]":
        return [clean(v) for v in value if clean(v)] if isinstance(value, (list, tuple)) else [clean(value)]
    return clean(value)


def row_hashes(rows: list[Row]) -> list[tuple[bytes, Row]]:
    """sha256 of each row's values, plus its occurrence number among identical
    rows. Registers publish exact duplicates (UKGC: 36 in licences); the
    occurrence keeps them distinct, so a duplicate disappearing is a change."""
    seen: dict[bytes, int] = {}
    out = []
    for r in rows:
        body = json.dumps([_jsonable(v) for v in r.values()], ensure_ascii=False, separators=(",", ":"))
        base = hashlib.sha256(body.encode()).digest()
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append((hashlib.sha256(base + n.to_bytes(4, "big")).digest(), r))
    return out


def _jsonable(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


class _PriorRuns:
    """Recent snapshots' manifests, newest first, loaded only when asked.

      carry(part)          a 304 means "the bytes you already have are
                           current": take them from the newest run — the one
                           whose ETag we sent — and check they still hash to
                           what was recorded
      baseline_rows(table) the row count of the last version that passed,
                           from the newest manifest that records one
    """

    def __init__(self, store: BlobStore, history: list[dict[str, Any]]) -> None:
        self._store = store
        self._history = history
        self._loaded: dict[int, dict[str, Any]] = {}

    @property
    def latest_blob_ref(self) -> str | None:
        return self._history[0].get("blob_ref") if self._history else None

    def carry(self, name: str) -> tuple[dict[str, Any], bytes] | None:
        if not self._history:
            return None
        entry = next((p for p in self._manifest(0).get("parts", []) if p.get("part") == name), None)
        if not entry or not entry.get("key") or not entry.get("sha256") or entry.get("problems"):
            return None
        try:
            raw = self._store.get(entry["key"])
        except Exception as exc:  # noqa: BLE001 — OSError locally, ClientError on S3; refetch either way
            log.warning("%s: previous blob %s unreadable (%s)", name, entry["key"], exc)
            return None
        if canon.sha256(raw).hex() != entry["sha256"]:
            log.error("%s: stored blob %s no longer matches its recorded digest", name, entry["key"])
            return None
        return entry, raw

    def baseline_rows(self, table: str) -> int | None:
        for i in range(len(self._history)):
            t = self._manifest(i).get("tables", {}).get(table)
            if t and t.get("baseline_rows") is not None:
                return int(t["baseline_rows"])
        return None

    def _manifest(self, i: int) -> dict[str, Any]:
        if i not in self._loaded:
            ref = self._history[i].get("blob_ref")
            try:
                self._loaded[i] = json.loads(self._store.get(ref)) if ref else {}
            except Exception as exc:  # noqa: BLE001
                log.warning("previous manifest %s unreadable (%s)", ref, exc)
                self._loaded[i] = {}
        return self._loaded[i]


def ingest_many(registers: list[Register], store: BlobStore, *, force: bool = False,
                accept_count_delta: bool = False) -> list[IngestResult]:
    """Run each register in turn. One register failing — even before it could
    write a row, e.g. the database dropping mid-run — does not stop the rest."""
    results = []
    for register in registers:
        try:
            results.append(ingest(register, store, force=force, accept_count_delta=accept_count_delta))
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: ingest failed before recording a row", register.slug)
            results.append(IngestResult(register.slug, None, False,
                                        f"UNRECORDED:{type(exc).__name__}:{exc}"[:500], 0, 0))
    return results
