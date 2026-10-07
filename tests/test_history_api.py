"""Phase 4 against the engine's own history: three gb_ukgc runs (a baseline, a
change, a failure) read back through the change feed and the snapshot list
(plan.md T4.3.a, T4.5.a-b). Skipped unless REGISTERWATCH_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from registerwatch import api
from registerwatch.http import deps
from tests.test_postgres import DSN, db, three_engine_runs  # noqa: F401 — db is a fixture

pytestmark = pytest.mark.skipif(not DSN, reason="REGISTERWATCH_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def runs(db, tmp_path_factory):  # noqa: F811
    cfg = SimpleNamespace(ingest_token="ingest-token", read_token="", stale_after_h=26.0, log_level="WARNING")
    with pytest.MonkeyPatch.context() as mp:
        first, second, third = three_engine_runs(db, tmp_path_factory.mktemp("blobs"), mp)
        mp.setattr(deps, "settings", lambda: cfg)
        mp.setattr(deps, "tx", db)
        with TestClient(api.app) as c:
            yield c, first, second, third


def test_the_feed_is_the_engines_history(runs):  # T4.3.a
    client, first, second, third = runs
    events = client.get("/v1/registers/gb_ukgc/changes").json()
    assert events["pagination"]["has_more"] is False
    got = [(e["change"], e["row"]["values"]["licence_number"], e["row"]["values"]["status"]) for e in events["data"]]
    # The baseline (run 1) is not a change; run 2 revoked 103 and renumbered 102; run 3 failed and changed nothing.
    assert sorted(got) == sorted([("removed", "000102-N-317976-010", "Active"),
                                  ("removed", "000103-R-100000-001", "Active"),
                                  ("added", "000102-N-317976-011", "Active"),
                                  ("added", "000103-R-100000-001", "Revoked")])
    assert {e["snapshot_id"] for e in events["data"]} == {second.snapshot_id}
    assert all(e["register"] == "gb_ukgc" and e["table"] == "licences" for e in events["data"])
    keys = [(e["snapshot_id"], e["row"]["id"], e["change"]) for e in events["data"]]
    assert keys == sorted(keys)
    # A window that ends before run 2 is empty; one that starts after it too.
    at = events["data"][0]["at"]
    assert client.get(f"/v1/registers/gb_ukgc/changes?until={at}").json()["data"] == []
    later = client.get("/v1/registers/gb_ukgc/changes?since=2999-01-01T00:00:00Z").json()
    assert later["data"] == [] and later["pagination"]["has_more"] is False


def test_snapshots_newest_first_with_reasons(runs):  # T4.5.a
    client, first, second, third = runs
    body = client.get("/v1/registers/gb_ukgc/snapshots").json()
    snaps = body["data"]
    assert [s["id"] for s in snaps] == [third.snapshot_id, second.snapshot_id, first.snapshot_id]
    assert [s["complete"] for s in snaps] == [False, True, True]
    assert snaps[0]["incomplete_reason"] == third.reason and snaps[0]["incomplete_reason"]
    assert snaps[0]["record_count"] is None and snaps[1]["record_count"] == second.record_count
    assert snaps[1]["incomplete_reason"] is None and all(s["register"] == "gb_ukgc" for s in snaps)
    assert body["pagination"]["has_more"] is False


def test_snapshots_page_one_at_a_time(runs):  # T4.5.b
    client, first, second, third = runs
    url, seen, pages = "/v1/registers/gb_ukgc/snapshots?limit=1", [], []
    r = client.get(url)
    while True:
        page = r.json()
        pages.append(page["pagination"]["has_more"])
        seen += [s["id"] for s in page["data"]]
        if not page["pagination"]["has_more"]:
            break
        r = client.get(url + "&cursor=" + page["pagination"]["next_cursor"])
    assert pages == [True, True, False]
    assert seen == [third.snapshot_id, second.snapshot_id, first.snapshot_id]
    assert client.get("/v1/registers/nope/snapshots").status_code == 404
