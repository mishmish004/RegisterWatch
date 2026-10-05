"""The engine, offline: real logic, fake HTTP, fake DB, real disk.

Driven through the GB register because its four-file shape exercises every
path, but nothing here is GB-specific. What the engine owns:

  1. discovery is fetched, logged and stored; a page that lost a link makes
     that part NOT_LISTED, not a smaller snapshot
  2. a part that 304s carries yesterday's bytes, checked against their digest
  3. a 200 that is not the register is stored but never counted
  4. a parsed table that is too small, blank where it must not be, or moved
     too far from the last good run makes the snapshot incomplete
  5. only a complete snapshot reaches the register's tables
  6. a crash still writes a row
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json

import pytest

from registerwatch.fetch.http import Fetched, Request
from registerwatch.ingest import engine
from registerwatch.registers import gb_ukgc
from registerwatch.storage.blobs import LocalBlobs

from tests.conftest import FIXTURES

REG = gb_ukgc.REGISTER
CSV = "https://www.gamblingcommission.gov.uk/downloads/business-licence-register-{}.csv"
LIC = CSV.format("licences")

LICENCES = (
    b"Account Number,Licence Number,Status,Type,Activity,Start Date,End Date\n"
    b'"102","000102-N-317976-010","Active","Non-Remote","Bingo","2014-03-12T00:00:00.000Z",\n'
    b'"103","000103-R-100000-001","Active","Remote","Casino","2015-01-01T00:00:00.000Z",\n'
    b'"104","000104-R-100001-002","Surrendered","Remote","Casino","2015-01-01T00:00:00.000Z","2020-01-01T00:00:00.000Z"\n'
)
PARTS = {
    "businesses": b'Account Number,Licence Account Name\n"102","Lucky Jacks Ltd"\n"103","1st Class Bet Ltd"\n'
                  b'"104","Harbour Park Ltd"\n',
    "licences": LICENCES,
    "trading-names": b'Account Number,Trading Name,Status\n"102","lucky jacks bingo","Active"\n'
                     b'"103","first class","Inactive"\n',
    "domain-names": b'Account Number,Domain Name,Status\n"102","luckyjacks.co.uk","Active"\n'
                    b'"103","firstclass.com","White Label"\n',
}
PAGE = "".join(f'<a href="/downloads/business-licence-register-{p}.csv">{p}</a>' for p in PARTS).encode()


class FakeDB:
    def __init__(self):
        self.rows: list[dict] = []
        self.history: list[dict] = []
        self.applied: list[tuple[int, dict]] = []

    @contextlib.contextmanager
    def tx(self):
        yield object()

    def insert_snapshot(self, conn, **kw):
        self.rows.append(kw)
        sid = len(self.rows)
        self.history.insert(0, {"id": sid, "blob_ref": kw["blob_ref"], "raw_hash": kw["raw_hash"],
                                "canonical_hash": kw["canonical_hash"]})
        return sid


@pytest.fixture
def h(tmp_path, monkeypatch):
    db = FakeDB()
    store = LocalBlobs(tmp_path)
    responses: dict[str, tuple[int, bytes | None] | list] = {}
    calls: list[tuple[str, dict | None]] = []

    monkeypatch.setattr(engine, "tx", db.tx)
    monkeypatch.setattr(engine.repo, "upsert_source", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(engine.repo, "fetched_recently", lambda *a, **k: False)
    monkeypatch.setattr(engine.repo, "etag_map", lambda *a, **k: {})
    monkeypatch.setattr(engine.repo, "recent_snapshots", lambda *a, **k: list(db.history))
    monkeypatch.setattr(engine.repo, "insert_snapshot", db.insert_snapshot)
    monkeypatch.setattr(engine.limiter, "relax", lambda host: None)
    monkeypatch.setattr(engine.observations, "apply",
                        lambda conn, reg, sid, rows: db.applied.append((sid, rows)))

    def fake_fetch_one(client, req: Request, budget, validators=None):
        calls.append((req.url, validators))
        spec = responses.get(req.url, (404, None))
        if isinstance(spec, list):
            spec = spec.pop(0) if len(spec) > 1 else spec[0]
        status, body = spec
        digest = hashlib.sha256(body).digest() if body is not None else None
        headers = {"content-type": "text/csv; charset=UTF-8", "etag": f'W/"{req.label}"'}
        log = [{"url": req.url, "label": req.label, "status_code": status, "etag": headers["etag"]}]
        return Fetched(req, status, body, digest, headers, log, not_modified=status == 304)

    monkeypatch.setattr(engine, "fetch_one", fake_fetch_one)
    responses[gb_ukgc.DOWNLOAD_URL] = (200, PAGE)
    for part, body in PARTS.items():
        responses[CSV.format(part)] = (200, body)

    class H:
        pass

    x = H()
    x.db, x.store, x.responses, x.tmp, x.calls = db, store, responses, tmp_path, calls
    x.run = lambda **kw: engine.ingest(REG, store, force=True, **kw)
    return x


def _manifest(h, result):
    return json.loads(h.store.get(result.blob_ref))


def _part(h, result, name):
    return next(p for p in _manifest(h, result)["parts"] if p["part"] == name)


def _all_304(h):
    for url in list(h.responses):
        if url != gb_ukgc.DOWNLOAD_URL:
            h.responses[url] = (304, None)


# --- 1. the whole path --------------------------------------------------------

def test_happy_path_stores_parses_records_and_applies(h):
    r = h.run()
    assert r.complete and r.reason is None and r.parts_ok == 4, r.reason
    assert r.tables == {"businesses": 3, "licences": 3, "trading_names": 2, "domain_names": 2}
    assert sorted(p.name for p in h.tmp.rglob("*.csv")) == \
        ["businesses.csv", "domain-names.csv", "licences.csv", "trading-names.csv"]
    assert list(h.tmp.rglob("_discovery-01.html"))  # the index page is evidence too

    row = h.db.rows[0]
    assert row["complete"] is True and row["incomplete_reason"] is None
    assert row["record_count"] == 10 and row["canonical_hash"] == r.canonical_hash and row["parsed"]

    (sid, rows), = h.db.applied
    assert sid == 1 and set(rows) == {"businesses", "licences", "trading_names", "domain_names"}
    digest, lic = rows["licences"][0]
    assert len(digest) == 32 and lic["licence_number"] == "000102-N-317976-010"
    assert str(lic["start_date"]) == "2014-03-12"

    m = _manifest(h, r)
    assert m["tables"]["licences"] == {"rows": 3, "baseline_rows": 3, "problems": [], "warnings": []}
    assert m["discovery"][0]["url"] == gb_ukgc.DOWNLOAD_URL and m["discovery"][0]["key"]


def test_the_blob_is_the_bytes_as_fetched(h):
    churned = b"\xef\xbb\xbf" + LICENCES.replace(b"\n", b"\r\n")
    h.responses[LIC] = (200, churned)
    r = h.run()
    assert r.complete, r.reason
    assert next(h.tmp.rglob("licences.csv")).read_bytes() == churned
    assert _part(h, r, "licences")["sha256"] == hashlib.sha256(churned).hexdigest()


def test_cosmetic_churn_leaves_the_canonical_hash_alone_and_a_real_edit_moves_it(h):
    first = h.run()
    h.responses[LIC] = (200, b"\xef\xbb\xbf" + LICENCES.replace(b"\n", b"\r\n"))
    churn = h.run()
    h.responses[LIC] = (200, LICENCES.replace(b'"Surrendered"', b'"Revoked"'))
    edit = h.run()
    assert churn.raw_hash != first.raw_hash and churn.canonical_hash == first.canonical_hash
    assert churn.unchanged
    assert edit.canonical_hash != first.canonical_hash and not edit.unchanged


# --- 2. gaps and 304s -----------------------------------------------------------

def test_a_failed_part_is_a_gap_and_nothing_reaches_the_tables(h):
    h.responses[LIC] = (503, None)
    r = h.run()
    assert not r.complete and r.reason == "PAGE_GAP:3/4 licences:HTTP_503"
    assert h.db.rows[0]["http_status"] == 503 and h.db.rows[0]["record_count"] is None
    assert h.db.applied == []  # the other three files must not "remove" anything
    assert len(list(h.tmp.rglob("*.csv"))) == 3  # evidence is not all-or-nothing


def test_discovery_failure_still_records_the_attempt(h):
    h.responses[gb_ukgc.DOWNLOAD_URL] = (404, None)
    r = h.run()
    assert not r.complete and r.reason.startswith("DISCOVERY_FAILED:404")
    assert len(h.db.rows) == 1 and h.db.rows[0]["pages_ok"] == 0 and h.db.applied == []


def test_a_renamed_part_is_not_listed_and_the_new_file_is_kept(h):
    names = ("businesses", "licences-v2", "trading-names", "domain-names")
    h.responses[gb_ukgc.DOWNLOAD_URL] = (200, "".join(
        f'<a href="/downloads/business-licence-register-{p}.csv">x</a>' for p in names).encode())
    h.responses[CSV.format("licences-v2")] = h.responses.pop(LIC)
    r = h.run()
    assert not r.complete and r.reason == "PAGE_GAP:3/4 licences:NOT_LISTED"
    extra = _part(h, r, "licences-v2")
    assert extra["key"] and extra["expected"] is False


def test_a_304_carries_yesterdays_bytes(h):
    first = h.run()
    _all_304(h)
    second = h.run()
    assert second.complete, second.reason
    assert second.raw_hash == first.raw_hash and second.canonical_hash == first.canonical_hash
    assert second.unchanged
    lic = _part(h, second, "licences")
    assert lic["not_modified"] and lic["carried_from"] == first.blob_ref
    assert lic["key"] == _part(h, first, "licences")["key"]


def test_a_304_with_no_usable_predecessor_refetches_unconditionally(h):
    h.db.history = [{"id": 0, "blob_ref": "gone/manifest.json", "raw_hash": b"", "canonical_hash": None}]
    for part, body in PARTS.items():
        h.responses[CSV.format(part)] = [(304, None), (200, body)]
    r = h.run()
    assert r.complete, r.reason
    assert [v for url, v in h.calls if url == LIC] == [None, None]


def test_a_304_to_an_unconditional_request_is_a_failed_part(h):
    h.db.history = [{"id": 0, "blob_ref": "gone/manifest.json", "raw_hash": b"", "canonical_hash": None}]
    _all_304(h)
    r = h.run()
    assert not r.complete and "licences:NOT_MODIFIED_WITHOUT_PRIOR" in r.reason


def test_a_carried_blob_that_changed_on_disk_is_refetched(h):
    first = h.run()
    (h.tmp / _part(h, first, "licences")["key"]).write_bytes(LICENCES + b'"9","x","Active","R","C",,\n')
    h.responses[LIC] = [(304, None), (200, LICENCES)]
    second = h.run()
    assert second.complete and not _part(h, second, "licences")["not_modified"]


# --- 3/4. a 200 that is not the register ------------------------------------------

@pytest.mark.parametrize("body, finding", [
    (b"<!DOCTYPE html><html><body><h1>Service unavailable</h1></body></html>", "PAGE_GAP:3/4 licences:HTML_BODY"),
    (b"", "PAGE_GAP:3/4 licences:EMPTY"),
    (b"Account Number,Licence Number,Status,Type,Activity,Start Date,End Date\n", "INVALID licences:NO_ROWS:0<1"),
    (b'Account Number,Status\n"102","Active"\n', "INVALID licences:HEADER_MISMATCH"),
    (LICENCES + b'"105","short row"\n', "INVALID licences:RAGGED_ROWS:1"),
])
def test_a_200_that_is_not_the_register_is_stored_but_not_applied(h, body, finding):
    h.responses[LIC] = (200, body)
    r = h.run()
    assert not r.complete and r.reason.startswith(finding), r.reason
    assert h.db.applied == [] and h.db.rows[0]["incomplete_reason"] == r.reason
    if body:
        assert next(h.tmp.rglob("licences.csv")).read_bytes() == body


def test_blank_required_cells_are_a_problem_in_bulk_and_a_warning_alone(h):
    many = LICENCES.replace(b'"Active"', b'""')
    h.responses[LIC] = (200, many)
    assert "licences:MISSING:status:2/3" in h.run().reason


def test_a_collapsed_row_count_is_held_until_accepted(h):
    assert h.run().complete  # baseline: 3 licence rows
    h.responses[LIC] = (200, b"\n".join(LICENCES.split(b"\n")[:2]) + b"\n")  # 1 row
    held = h.run()
    assert not held.complete and "licences:COUNT_DELTA:3->1" in held.reason
    again = h.run()  # the held run did not become the new normal
    assert not again.complete and "COUNT_DELTA:3->1" in again.reason
    accepted = h.run(accept_count_delta=True)
    assert accepted.complete
    assert "COUNT_DELTA:3->1" in _manifest(h, accepted)["tables"]["licences"]["warnings"]
    assert h.run().complete  # 1 row is the baseline now
    assert [sid for sid, _ in h.db.applied] == [1, 4, 5]


def test_an_unknown_status_is_a_warning_not_a_failure(h):
    h.responses[LIC] = (200, LICENCES.replace(b'"Surrendered"', b'"Under Review"'))
    r = h.run()
    assert r.complete
    assert _manifest(h, r)["tables"]["licences"]["warnings"] == ["UNKNOWN_VALUE:status:Under Review"]


# --- 6. crashes -------------------------------------------------------------------

def test_a_crash_mid_run_still_writes_a_row(h, monkeypatch):
    real_put = h.store.put

    def put(key, data, content_type="application/octet-stream"):
        if key.endswith("trading-names.csv"):
            raise OSError("No space left on device")
        return real_put(key, data, content_type)

    monkeypatch.setattr(h.store, "put", put)
    r = h.run()
    assert not r.complete and r.reason.startswith("EXCEPTION:OSError:No space left on device")
    assert h.db.rows[0]["incomplete_reason"] == r.reason and h.db.applied == []
    assert [p["part"] for p in _manifest(h, r)["parts"]] == ["businesses", "licences"]


@pytest.mark.parametrize("parse, finding", [
    (lambda b: [{"oops": 1}], "businesses:UNDECLARED_COLUMNS"),
    (lambda b: 1 / 0, "businesses:PARSER_BUG:ZeroDivisionError"),
])
def test_a_parser_bug_is_recorded_not_raised(h, parse, finding):
    table = next(t for t in REG.tables if t.name == "businesses")
    broken = dataclasses.replace(REG, tables=(dataclasses.replace(table, parse=parse),))
    r = engine.ingest(broken, h.store, force=True)
    assert not r.complete and finding in r.reason and h.db.applied == []


def test_ingest_many_keeps_going_after_a_register_fails(h, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("db down")

    real = engine.ingest
    monkeypatch.setattr(engine, "ingest", lambda reg, *a, **k: boom() if reg.slug == "be_gc" else real(reg, *a, **k))
    from registerwatch.registers import REGISTRY
    out = engine.ingest_many([REGISTRY["be_gc"], REG], h.store, force=True)
    assert out[0].reason.startswith("UNRECORDED:ConnectionError") and out[1].complete


def test_skip_when_a_good_snapshot_is_recent(h, monkeypatch):
    monkeypatch.setattr(engine.repo, "fetched_recently", lambda *a, **k: True)
    r = engine.ingest(REG, h.store)
    assert r.skipped and h.db.rows == [] and h.calls == []


# --- row identity -------------------------------------------------------------------

def test_exact_duplicate_rows_keep_distinct_identities():
    rows = [{"a": "x"}, {"a": "x"}, {"a": "y"}]
    hashes = [d for d, _ in engine.row_hashes(rows)]
    assert len(set(hashes)) == 3
    assert engine.row_hashes([{"a": "x"}])[0][0] == hashes[0]  # stable across runs


def test_fixture_index_page_lists_the_four_csvs():
    found = gb_ukgc.asset_links(gb_ukgc.DOWNLOAD_URL, (FIXTURES / "ukgc_download.html").read_bytes())
    assert set(gb_ukgc.EXPECTED_PARTS) <= set(found)
    assert not any(u.endswith((".zip", ".xlsx")) for u in found.values())


def test_an_html_part_answered_with_json_says_so():
    from registerwatch.ingest import validate

    health = b'{"check_db":"pass","check_nginx":"pass","check_php":"pass"}'
    assert validate.check_part(health, "html", "application/json") == ["CONTENT_TYPE:application/json"]
    assert validate.check_part(b"<html></html>", "html", "text/html; charset=UTF-8") == []


def test_a_wrong_kind_of_answer_is_retried_once_in_the_same_run(h):
    page = b"<!DOCTYPE html><html><body>Service unavailable</body></html>"
    h.responses[LIC] = [(200, page), (200, LICENCES)]
    r = h.run()
    assert r.complete, r.reason
    lic = _part(h, r, "licences")
    assert lic["rejected"]["problems"] == ["HTML_BODY"] and lic["key"].endswith("licences.retry-1.csv")
    assert (h.tmp / lic["rejected"]["key"]).read_bytes() == page  # the rejected answer is still evidence
    assert [v for url, v in h.calls if url == LIC] == [None, None]


def test_a_content_problem_is_not_retried(h):
    h.responses[LIC] = [(200, b'Account Number,Status\n"102","Active"\n'), (200, LICENCES)]
    r = h.run()
    assert not r.complete and "HEADER_MISMATCH" in r.reason
    assert len([u for u, _ in h.calls if u == LIC]) == 1


def test_a_register_can_ask_for_a_longer_timeout():
    from registerwatch.registers import REGISTRY
    assert REGISTRY["pl_mf"].timeout_s == 180 and REGISTRY["gb_ukgc"].timeout_s is None
