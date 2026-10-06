"""Facts -> the model: one row per thing, and the changes to it.

For each register, every fact is grouped by (kind, key). The facts in a group
are the versions of one thing; their RowRefs say when each version was current.
From a group the build makes:

  a model row   the newest current version's values (or the last version's,
                when nothing is current any more), first seen, last seen,
                removed, and the evidence: every register row behind it
  events        walking the snapshots at which the group changed, comparing
                the set of values current before and after each one:

                  nothing -> something     <kind>.added
                  something -> nothing     <kind>.removed
                  something -> different   <kind>.status_changed if the
                                           published status moved, otherwise
                                           <kind>.changed, with the fields
                                           before and after

Comparing values rather than rows is what makes the feed quiet: a row that
changed only in a column no fact reads produces no event, and UKGC's licence
renumbering is one `licence.changed` (reference ...-010 -> ...-011), not a
revocation and a grant.

A register's first snapshot is its baseline. Facts it first saw there are the
state of the world when watching began, not news, so they raise no `added`
event; the same holds for a table first loaded after its register (its first
snapshot is its own baseline).

Pure: no database, no clock. The same inputs build the same model, ids
included, so a rebuild replaces rows with themselves.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from registerwatch import jurisdictions
from registerwatch.model import domains, names
from registerwatch.model.facts import Fact, RowRef, merge_products, status, thaw
from registerwatch.model.projections import Rows, project
from registerwatch.registers.base import Register

EVIDENCE_LIMIT = 20  # newest register rows kept on a model row; `versions` counts them all


@dataclass
class RegisterInput:
    register: Register
    rows: Rows
    snapshot_times: Mapping[int, datetime] = field(default_factory=dict)


@dataclass
class Model:
    parties: list[dict[str, Any]] = field(default_factory=list)
    licences: list[dict[str, Any]] = field(default_factory=list)
    brands: list[dict[str, Any]] = field(default_factory=list)
    domains: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def tables(self) -> dict[str, list[dict[str, Any]]]:
        return {"parties": self.parties, "licences": self.licences, "brands": self.brands,
                "domains": self.domains, "blocks": self.blocks, "events": self.events}


def build(inputs: Iterable[RegisterInput]) -> Model:
    model = Model()
    for inp in sorted(inputs, key=lambda i: i.register.slug):
        _Register(inp, model).run()
    for name, rows in model.tables().items():
        if name == "events":
            rows.sort(key=lambda e: (e["at"] is None, e["at"] or _EPOCH, e["id"]))
        else:
            rows.sort(key=lambda r, col=_ID[_KIND[name]]: r[col])
    return model


def model_id(slug: str, key: str) -> str:
    return f"{slug}:{key}"


class _Register:
    def __init__(self, inp: RegisterInput, model: Model) -> None:
        self.reg = inp.register
        self.slug = inp.register.slug
        self.jur = jurisdictions.normalise(inp.register.country)
        self.times = inp.snapshot_times
        self.model = model
        self.baselines = {t: min(ref.first_snapshot for ref, _ in rows)
                          for t, rows in inp.rows.items() if rows}
        self.groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
        for f in project(self.slug, inp.rows):
            self.groups[(f.kind, f.key)].append(f)
        self.names: dict[str, str | None] = {}

    def run(self) -> None:
        counts = defaultdict(int)
        # Parties first: every other kind's summary names its party.
        for kind in ("party", "licence", "brand", "domain", "block"):
            for (k, key), versions in sorted(self.groups.items()):
                if k != kind:
                    continue
                versions.sort(key=lambda f: (f.ref.first_snapshot, f.ref.row_id))
                row = getattr(self, f"_{kind}")(key, _latest(versions), versions)
                row.update(_lifetime(versions))
                getattr(self.model, _TABLE[kind]).append(row)
                counts[kind] += 1
                self._events(kind, key, versions, row)
        self.model.counts[self.slug] = dict(counts)

    # -- links -----------------------------------------------------------------------

    def _link(self, kind: str, key: str | None) -> str | None:
        return model_id(self.slug, key) if key and (kind, key) in self.groups else None

    # -- one model row per kind ------------------------------------------------------

    def _base(self) -> dict[str, Any]:
        return {"register": self.slug, "jurisdiction": self.jur}

    def _party(self, key: str, rep: Fact, versions: list[Fact]) -> dict[str, Any]:
        name = rep.get("name")
        nk = names.name_key(name)
        pid = model_id(self.slug, key)
        self.names[pid] = name
        ids: dict[str, str] = {}
        for v in versions:  # identifiers seen in any version, newest winning
            ids.update(thaw(v.get("identifiers")) or {})
        return {"party_id": pid, **self._base(), "regulator": self.reg.regulator, "name": name,
                "name_key": nk, "name_core": names.name_core(nk), "cluster_id": names.slug(nk) if nk else None,
                "identifiers": ids,
                "aliases": sorted({v.get("name") for v in versions if v.get("name") and v.get("name") != name})}

    def _licence(self, key: str, rep: Fact, versions: list[Fact]) -> dict[str, Any]:
        st, since = status(rep.get("status_raw"))
        v = rep.as_dict()
        return {"licence_id": model_id(self.slug, key), **self._base(), "party_id": self._link("party", v["party"]),
                "regulator": self.reg.regulator, "authority": v["authority"] or self.reg.regulator,
                "reference": v["reference"], "type": v["type"], "products": list(v["products"] or []),
                "channel": v["channel"], "area": v["area"], "site": v["site"], "status": st,
                "status_raw": v["status_raw"], "status_since": since, "valid_from": v["valid_from"],
                "valid_to": v["valid_to"], "notes": v["notes"]}

    def _brand(self, key: str, rep: Fact, versions: list[Fact]) -> dict[str, Any]:
        st, _ = status(rep.get("status_raw"))
        v = rep.as_dict()
        nk = names.name_key(v["name"])
        return {"brand_id": model_id(self.slug, key), **self._base(), "party_id": self._link("party", v["party"]),
                "name": v["name"], "name_key": nk, "status": st, "status_raw": v["status_raw"],
                "products": list(v["products"] or [])}

    def _domain(self, key: str, rep: Fact, versions: list[Fact]) -> dict[str, Any]:
        st, _ = status(rep.get("status_raw"))
        v = rep.as_dict()
        lic = self._link("licence", v["licence"])
        prods = v["products"] or []
        if not prods and lic:  # a listing under a licence offers what the licence permits
            lic_fact = _latest(self.groups[("licence", v["licence"])])
            prods = list(lic_fact.get("products") or ())
        return {"listing_id": model_id(self.slug, key), **self._base(), "host": v["host"],
                "registrable": domains.registrable(v["host"]), "label": domains.label(v["host"]),
                "published": v["published"], "party_id": self._link("party", v["party"]),
                "brand_id": self._link("brand", v["brand"]), "licence_id": lic, "status": st,
                "status_raw": v["status_raw"], "products": list(merge_products(prods)), "since": v["since"]}

    def _block(self, key: str, rep: Fact, versions: list[Fact]) -> dict[str, Any]:
        v = rep.as_dict()
        return {"block_id": model_id(self.slug, key), **self._base(), "regulator": self.reg.regulator,
                "host": v["host"], "registrable": domains.registrable(v["host"]), "label": domains.label(v["host"]),
                "published": v["published"], "listed_on": v["listed_on"]}

    # -- events --------------------------------------------------------------------------

    def _events(self, kind: str, key: str, versions: list[Fact], row: dict[str, Any]) -> None:
        points = sorted({v.ref.first_snapshot for v in versions}
                        | {v.ref.removed_snapshot for v in versions if v.ref.removed_snapshot is not None})
        before: frozenset = frozenset()
        for p in points:
            now = frozenset(v.values for v in versions
                            if v.ref.first_snapshot <= p and (v.ref.removed_snapshot is None or v.ref.removed_snapshot > p))
            if now == before:
                continue
            arrived = [v for v in versions if v.ref.first_snapshot == p]
            if not before and arrived and all(self.baselines.get(v.ref.table) == p for v in arrived):
                before = now  # the baseline: what was there when watching began
                continue
            self.model.events.append(self._event(kind, key, p, before, now, versions, row))
            before = now

    def _event(self, kind: str, key: str, p: int, old: frozenset, new: frozenset, versions: list[Fact],
               row: dict[str, Any]) -> dict[str, Any]:
        olds = [self._public(kind, dict(v)) for v in sorted(old, key=repr)]
        news = [self._public(kind, dict(v)) for v in sorted(new, key=repr)]
        if not old:
            etype, b, a = "added", None, (news[0] if len(news) == 1 else news)
        elif not new:
            etype, b, a = "removed", (olds[0] if len(olds) == 1 else olds), None
        elif len(olds) == 1 and len(news) == 1:
            diff = [f for f in news[0] if olds[0].get(f) != news[0].get(f)]
            etype = "status_changed" if "status_raw" in diff else "changed"
            b, a = {f: olds[0].get(f) for f in diff}, {f: news[0].get(f) for f in diff}
        else:
            etype, b, a = "changed", olds, news
        subject = row.get(f"{_ID[kind]}")
        party_id = row.get("party_id") if kind != "party" else subject
        at = self._time(p, versions)
        return {
            "id": hashlib.sha256(f"{self.slug}|{kind}|{key}|{p}|{etype}".encode()).hexdigest()[:20],
            "at": at, "snapshot_id": p, **self._base(), "kind": kind, "type": f"{kind}.{etype}",
            "subject_id": subject, "party_id": party_id, "host": row.get("host"),
            "before": b, "after": a,
            "summary": self._summary(kind, etype, row, b, a),
        }

    def _time(self, p: int, versions: list[Fact]) -> datetime | None:
        if p in self.times:
            return self.times[p]
        stamps = [v.ref.first_seen_at for v in versions if v.ref.first_snapshot == p and v.ref.first_seen_at]
        stamps += [v.ref.removed_at for v in versions if v.ref.removed_snapshot == p and v.ref.removed_at]
        return min(stamps) if stamps else None

    def _public(self, kind: str, values: dict[str, Any]) -> dict[str, Any]:
        """Fact values as an event shows them: register-local link keys become
        model ids, frozen tuples become lists and dicts."""
        out = {}
        for k, v in values.items():
            if k in ("party", "licence", "brand") and kind != "party":
                out[f"{k}_id"] = model_id(self.slug, v) if v else None
            else:
                out[k] = thaw(v)
        return out

    def _summary(self, kind: str, etype: str, row: dict[str, Any], before: Any, after: Any) -> str:
        who = self.names.get(row.get("party_id")) if kind != "party" else None
        what = {
            "party": lambda: row.get("name") or "a party",
            "licence": lambda: " ".join(x for x in (
                "licence", row.get("reference") or "", f"({row['type']})" if row.get("type") else "",
                f"of {who}" if who else "") if x),
            "brand": lambda: f"trading name {row.get('name')}" + (f" of {who}" if who else ""),
            "domain": lambda: f"{row.get('host') or row.get('published')}" + (f" listed for {who}" if who else ""),
            "block": lambda: f"{row.get('host') or row.get('published')}",
        }[kind]()
        lead = f"{self.jur} · {self.reg.regulator}: {what}"
        if etype == "added":
            return f"{lead} — {'added to the blocklist' if kind == 'block' else 'appeared on the register'}"
        if etype == "removed":
            return f"{lead} — {'removed from the blocklist' if kind == 'block' else 'removed from the register'}"
        if isinstance(before, dict) and isinstance(after, dict):
            parts = [f"{f.replace('_raw', '').replace('_id', '')} {_short(before[f])} → {_short(after[f])}"
                     for f in after]
            return f"{lead} — {'; '.join(parts)}"
        return f"{lead} — changed"


_TABLE = {"party": "parties", "licence": "licences", "brand": "brands", "domain": "domains", "block": "blocks"}
_KIND = {t: k for k, t in _TABLE.items()}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ID = {"party": "party_id", "licence": "licence_id", "brand": "brand_id", "domain": "listing_id", "block": "block_id"}


def _latest(versions: list[Fact]) -> Fact:
    """The version that speaks for the group: the newest current one, else the
    one removed last."""
    current = [v for v in versions if v.ref.current]
    if current:
        return max(current, key=lambda v: (v.ref.first_snapshot, v.ref.row_id))
    return max(versions, key=lambda v: (v.ref.removed_snapshot or 0, v.ref.first_snapshot, v.ref.row_id))


def _lifetime(versions: list[Fact]) -> dict[str, Any]:
    refs = sorted({v.ref for v in versions}, key=lambda r: (r.first_snapshot, r.row_id), reverse=True)
    current = any(r.current for r in refs)
    firsts = [r.first_seen_at for r in refs if r.first_seen_at]
    lasts = [r.last_seen_at for r in refs if r.last_seen_at]
    removed = [r.removed_at for r in refs if r.removed_at]
    return {
        "current": current,
        "first_seen_at": min(firsts) if firsts else None,
        "last_seen_at": max(lasts) if lasts else None,
        "removed_at": None if current else (max(removed) if removed else None),
        "versions": len(refs),
        "evidence": [_ref(r) for r in refs[:EVIDENCE_LIMIT]],
    }


def _ref(r: RowRef) -> dict[str, Any]:
    return {"table": r.table, "row_id": r.row_id, "first_snapshot": r.first_snapshot,
            "removed_snapshot": r.removed_snapshot, "first_seen_at": r.first_seen_at,
            "last_seen_at": r.last_seen_at, "removed_at": r.removed_at}


def _short(v: Any) -> str:
    if v is None or v == [] or v == "":
        return "—"
    s = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
    return s if len(s) <= 60 else s[:59] + "…"
