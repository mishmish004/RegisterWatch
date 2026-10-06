"""Read the model back: the questions customers ask, answered from one place.
Shared by the API and the CLI, like query.py is for the raw registers.

  domain             is this site allowed, where, under whom, since when — a
                     verdict per jurisdiction, the listings and blocks behind
                     it, the same name elsewhere, and its history
  search_operators   companies by name, trading name or website
  operator           one operator across every register: parties, licences,
                     brands, websites, its websites on blocklists, history
  licences           licences filtered by jurisdiction, status, product, text
  events             the change feed: what changed, where, since when
  jurisdiction       a jurisdiction's registers, what they cover, counts, news
  coverage           every register's coverage, freshness and data quality,
                     and the jurisdictions with no usable register

Every list a customer sees carries the register it came from; every row
carries `evidence`, the register rows behind it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any

from psycopg import Connection

from registerwatch import jurisdictions
from registerwatch.db.repos import model as model_repo
from registerwatch.db.repos import snapshots as snapshot_repo
from registerwatch.model import catalogue, domains, names, verdict
from registerwatch.model.facts import NOT_OPERATING, OPERATING, PRODUCTS
from registerwatch.registers import REGISTRY
from registerwatch.registers.base import Register

MAX_LIMIT = 500


def _slugs(registers: list[Register]) -> list[str]:
    return [r.slug for r in registers]


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


def _limit(n: int) -> int:
    return max(0, min(n, MAX_LIMIT))


def sources(conn: Connection, slugs: set[str]) -> dict[str, dict[str, Any]]:
    """Register metadata for the evidence beside a claim: who publishes it,
    where, and how fresh our copy is."""
    health = {h["slug"]: h for h in snapshot_repo.source_health(conn)}
    return {s: {"regulator": REGISTRY[s].regulator, "name": REGISTRY[s].name, "homepage": REGISTRY[s].homepage,
                "jurisdiction": jurisdictions.normalise(REGISTRY[s].country),
                "last_good": health.get(s, {}).get("last_good")} for s in sorted(slugs) if s in REGISTRY}


# --- domain -----------------------------------------------------------------------------

def domain(conn: Connection, value: str, registers: list[Register], *, today: date) -> dict[str, Any]:
    h = domains.host_key(value)
    if not h:
        raise ValueError(f"{value!r} is not a hostname")
    reg = domains.registrable(h)
    regs = _slugs(registers)
    where = "register = ANY(%(regs)s) AND (host = ANY(%(hosts)s) OR (%(reg)s::text IS NOT NULL AND registrable = %(reg)s))"
    p = {"regs": regs, "hosts": domains.parents(h), "reg": reg}
    listings = conn.execute(f"SELECT * FROM model.domains WHERE {where}", p).fetchall()
    blocks = conn.execute(f"SELECT * FROM model.blocks WHERE {where}", p).fetchall()

    party_ids = sorted({x["party_id"] for x in listings if x["party_id"]})
    lic_ids = sorted({x["licence_id"] for x in listings if x["licence_id"]})
    licences = {r["licence_id"]: r for r in conn.execute(
        "SELECT * FROM model.licences WHERE party_id = ANY(%s) OR licence_id = ANY(%s)", (party_ids, lic_ids))}
    parties = {r["party_id"]: r for r in conn.execute(
        "SELECT * FROM model.parties WHERE party_id = ANY(%s)", (party_ids,))}
    brands = {r["brand_id"]: r for r in conn.execute(
        "SELECT * FROM model.brands WHERE brand_id = ANY(%s)", (sorted({x["brand_id"] for x in listings if x["brand_id"]}),))}

    out = verdict.assess(h, listings=listings, blocks=blocks, licences=licences, parties=parties, brands=brands,
                         registers=registers, today=today)
    out["same_name_elsewhere"] = _same_label(conn, h, regs)
    out["history"] = conn.execute(
        "SELECT * FROM model.events WHERE register = ANY(%s) AND host = ANY(%s) ORDER BY at DESC, id LIMIT 100",
        (regs, domains.parents(h))).fetchall()
    out["sources"] = sources(conn, {m["register"] for v in out["verdicts"] for m in v["matches"]}
                             | {r for v in out["verdicts"] for r in v["registers"]})
    out["not_covered"] = [vars(u) for u in catalogue.UNCOVERED]
    return out


def _same_label(conn: Connection, h: str, regs: list[str], limit: int = 50) -> list[dict[str, Any]]:
    """Other registrable domains with the same label ("bet365" for bet365.com):
    bet365.es licensed in Spain, bet365.it blocked… A brand family by name, so
    a lead to check, not a link the registers make."""
    label, reg = domains.label(h), domains.registrable(h)
    if not label:
        return []
    return conn.execute(
        """
        SELECT kind, jurisdiction, host, bool_or(current) AS current,
               array_agg(DISTINCT register ORDER BY register) AS registers,
               min(party) AS party, min(operator_id) AS operator_id
          FROM (
            SELECT 'listing' AS kind, d.jurisdiction, d.register, d.host, d.current, p.name AS party,
                   p.cluster_id AS operator_id
              FROM model.domains d LEFT JOIN model.parties p ON p.party_id = d.party_id
             WHERE d.label = %(label)s AND d.registrable <> %(reg)s AND d.register = ANY(%(regs)s)
            UNION ALL
            SELECT 'block', b.jurisdiction, b.register, b.host, b.current, NULL, NULL
              FROM model.blocks b
             WHERE b.label = %(label)s AND b.registrable <> %(reg)s AND b.register = ANY(%(regs)s)
          ) x
         GROUP BY kind, jurisdiction, host
         ORDER BY kind = 'listing' DESC, bool_or(current) DESC, jurisdiction, host
         LIMIT %(limit)s
        """, {"label": label, "reg": reg, "regs": regs, "limit": limit}).fetchall()


# --- operators --------------------------------------------------------------------------

def search_operators(conn: Connection, q: str, registers: list[Register], *, limit: int = 20) -> dict[str, Any]:
    """Operators (parties clustered on name) matching a company name, a
    trading name or a website. Exact names first, then names that start with
    the query, then the widest footprint."""
    key = names.name_key(q) or ""
    core = names.name_core(key) or key
    if not core:  # only punctuation: it would match every name
        return {"q": q, "operators": [], "brands_without_operator": []}
    h = domains.host_key(q)
    p = {"like": _like(core), "core": core, "prefix": _like(core)[1:], "host": h,
         "label": domains.label(h) if h else None, "regs": _slugs(registers), "limit": _limit(limit)}
    rows = conn.execute(
        """
        WITH hits AS (
          SELECT cluster_id, 'name' AS via FROM model.parties WHERE name_key LIKE %(like)s
          UNION SELECT p.cluster_id, 'brand' FROM model.brands b JOIN model.parties p ON p.party_id = b.party_id
                 WHERE b.name_key LIKE %(like)s
          UNION SELECT p.cluster_id, 'website' FROM model.domains d JOIN model.parties p ON p.party_id = d.party_id
                 WHERE d.host = %(host)s OR d.label = %(label)s
        )
        SELECT p.cluster_id AS operator_id,
               mode() WITHIN GROUP (ORDER BY p.name) AS name,
               array_agg(DISTINCT p.jurisdiction ORDER BY p.jurisdiction) AS jurisdictions,
               count(DISTINCT p.party_id) AS parties,
               bool_or(p.current) AS current,
               (SELECT array_agg(DISTINCT via ORDER BY via) FROM hits h2 WHERE h2.cluster_id = p.cluster_id) AS matched_on
          FROM model.parties p
         WHERE p.cluster_id IN (SELECT cluster_id FROM hits) AND p.register = ANY(%(regs)s)
         GROUP BY p.cluster_id
         ORDER BY bool_or(p.name_core = %(core)s) DESC, bool_or(p.name_core LIKE %(prefix)s) DESC,
                  bool_or(p.current) DESC, count(DISTINCT p.jurisdiction) DESC, 2
         LIMIT %(limit)s
        """, p).fetchall()
    brands = conn.execute(
        """
        SELECT b.brand_id, b.name, b.jurisdiction, b.register, b.products, b.current
          FROM model.brands b
         WHERE b.party_id IS NULL AND b.register = ANY(%(regs)s) AND b.name_key LIKE %(like)s
         ORDER BY b.current DESC, b.name LIMIT %(limit)s
        """, p).fetchall()
    return {"q": q, "operators": rows, "brands_without_operator": brands}


def operator(conn: Connection, operator_id: str, registers: list[Register], *, today: date) -> dict[str, Any]:
    regs = _slugs(registers)
    parties = conn.execute(
        "SELECT * FROM model.parties WHERE (cluster_id = %s OR party_id = %s) AND register = ANY(%s) "
        "ORDER BY jurisdiction, register, name", (operator_id, operator_id, regs)).fetchall()
    if not parties:
        raise KeyError(f"no operator {operator_id!r}")
    if len({p["cluster_id"] for p in parties}) == 1 and parties[0]["cluster_id"] != operator_id:
        return operator(conn, parties[0]["cluster_id"], registers, today=today)  # a party id: show its cluster
    pids = [p["party_id"] for p in parties]
    licences = conn.execute("SELECT * FROM model.licences WHERE party_id = ANY(%s) "
                            "ORDER BY current DESC, jurisdiction, type, reference", (pids,)).fetchall()
    brands = conn.execute("SELECT * FROM model.brands WHERE party_id = ANY(%s) ORDER BY current DESC, jurisdiction, name",
                          (pids,)).fetchall()
    listings = conn.execute("SELECT * FROM model.domains WHERE party_id = ANY(%s) "
                            "ORDER BY current DESC, jurisdiction, host", (pids,)).fetchall()
    registrables = sorted({d["registrable"] for d in listings if d["registrable"]})
    blocked = conn.execute("SELECT * FROM model.blocks WHERE registrable = ANY(%s) AND register = ANY(%s) "
                           "ORDER BY current DESC, jurisdiction, host", (registrables, regs)).fetchall()
    related = conn.execute(
        """
        SELECT p.cluster_id AS operator_id, mode() WITHIN GROUP (ORDER BY p.name) AS name,
               array_agg(DISTINCT d.registrable ORDER BY d.registrable) AS shared_domains,
               array_agg(DISTINCT d.jurisdiction ORDER BY d.jurisdiction) AS jurisdictions
          FROM model.domains d JOIN model.parties p ON p.party_id = d.party_id
         WHERE d.registrable = ANY(%s) AND p.cluster_id <> %s AND d.current
         GROUP BY p.cluster_id ORDER BY count(DISTINCT d.registrable) DESC LIMIT 50
        """, (registrables, operator_id)).fetchall()
    history = conn.execute("SELECT * FROM model.events WHERE party_id = ANY(%s) ORDER BY at DESC, id LIMIT 200",
                           (pids,)).fetchall()

    footprint = []
    for code in sorted({p["jurisdiction"] for p in parties}):
        lj = [x for x in licences if x["jurisdiction"] == code and x["current"]]
        footprint.append({
            "jurisdiction": code, "name": jurisdictions.name(code),
            "parties": [p["name"] for p in parties if p["jurisdiction"] == code and p["current"]],
            "licences": dict(Counter(x["status"] for x in lj)),
            "products": sorted({pr for x in lj for pr in x["products"]}, key=PRODUCTS.index),
            "websites": len({d["host"] for d in listings if d["jurisdiction"] == code and d["current"] and d["host"]}),
            "listed": any(p["current"] for p in parties if p["jurisdiction"] == code),
        })
    names_ = Counter(p["name"] for p in parties if p["current"]) or Counter(p["name"] for p in parties)
    return {
        "operator_id": parties[0]["cluster_id"], "name": names_.most_common(1)[0][0],
        "matched_on": "name", "footprint": footprint, "flags": _flags(licences, listings, blocked, today),
        "parties": parties, "licences": licences, "brands": brands, "websites": listings,
        "websites_blocked": blocked, "related_operators": related, "history": history,
        "sources": sources(conn, {p["register"] for p in parties} | {b["register"] for b in blocked}),
    }


def _flags(licences: list[dict], listings: list[dict], blocked: list[dict], today: date) -> list[dict[str, Any]]:
    """What deserves a second look: the grey areas a status alone hides."""
    out = []
    for x in licences:
        if not x["current"]:
            continue
        if x["status"] in NOT_OPERATING or x["status"] == "pending":
            out.append({"kind": "licence_status", "jurisdiction": x["jurisdiction"], "licence_id": x["licence_id"],
                        "text": f"{x['jurisdiction']}: licence {x['reference'] or x['type']} is "
                                f"{x['status_raw'] or x['status']}"})
        elif x["status"] in OPERATING and x["valid_to"] and x["valid_to"] < today:
            out.append({"kind": "end_date_passed", "jurisdiction": x["jurisdiction"], "licence_id": x["licence_id"],
                        "text": f"{x['jurisdiction']}: licence {x['reference'] or x['type']} ended {x['valid_to']} "
                                "but is still listed"})
    listed: dict[str, set[str]] = defaultdict(set)
    for d in listings:
        if d["current"] and d["host"]:
            listed[d["host"]].add(d["jurisdiction"])
    pairs = sorted({(b["host"], b["jurisdiction"]) for b in blocked if b["current"] and b["host"] in listed})
    for host, where in pairs:  # one flag per host and blocking jurisdiction, however many lists name it
        out.append({"kind": "listed_and_blocked", "jurisdiction": where, "host": host,
                    "text": f"{host} is listed in {', '.join(sorted(listed[host]))} and blocked in {where}"})
    return out


# --- licences and events ------------------------------------------------------------------

def licences(conn: Connection, registers: list[Register], *, status: str | None = None, product: str | None = None,
             q: str | None = None, current: bool | None = True, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    where, p = ["l.register = ANY(%(regs)s)"], {"regs": _slugs(registers)}
    if status:
        where.append("l.status = ANY(%(status)s)")
        p["status"] = status.split(",")
    if product:
        where.append("l.products && %(product)s")
        p["product"] = product.split(",")
    if current is not None:
        where.append("l.current = %(current)s")
        p["current"] = current
    if q:
        where.append("(l.reference ILIKE %(q)s OR l.type ILIKE %(q)s OR pa.name ILIKE %(q)s)")
        p["q"] = _like(q)
    w = " AND ".join(where)
    base = f"FROM model.licences l LEFT JOIN model.parties pa ON pa.party_id = l.party_id WHERE {w}"
    total = conn.execute(f"SELECT count(*) AS n {base}", p).fetchone()["n"]
    rows = conn.execute(
        f"SELECT l.*, pa.name AS party_name, pa.cluster_id AS operator_id {base} "
        "ORDER BY l.jurisdiction, pa.name, l.type, l.licence_id LIMIT %(limit)s OFFSET %(offset)s",
        {**p, "limit": _limit(limit), "offset": max(0, offset)}).fetchall()
    return {"total": total, "limit": _limit(limit), "offset": offset, "licences": rows}


def events(conn: Connection, registers: list[Register], *, since: datetime | None = None,
           until: datetime | None = None, types: list[str] | None = None, operator_id: str | None = None,
           host: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Newest first. `types` takes full types (licence.status_changed) or kinds
    (licence, block); `host` matches a listing's or block's host and its
    subdomains."""
    where, p = ["e.register = ANY(%(regs)s)"], {"regs": _slugs(registers)}
    if since:
        where.append("e.at >= %(since)s")
        p["since"] = since
    if until:
        where.append("e.at < %(until)s")
        p["until"] = until
    if types:
        where.append("(e.type = ANY(%(types)s) OR e.kind = ANY(%(types)s))")
        p["types"] = types
    if operator_id:
        where.append("e.party_id IN (SELECT party_id FROM model.parties WHERE cluster_id = %(op)s OR party_id = %(op)s)")
        p["op"] = operator_id
    if host:
        h = domains.host_key(host)
        if not h:
            raise ValueError(f"{host!r} is not a hostname")
        where.append("(e.host = %(host)s OR e.host LIKE %(sub)s)")
        p.update(host=h, sub="%." + h)
    w = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) AS n FROM model.events e WHERE {w}", p).fetchone()["n"]
    rows = conn.execute(f"SELECT e.* FROM model.events e WHERE {w} ORDER BY e.at DESC NULLS LAST, e.id "
                        "LIMIT %(limit)s OFFSET %(offset)s",
                        {**p, "limit": _limit(limit), "offset": max(0, offset)}).fetchall()
    by_type = conn.execute(f"SELECT e.type, count(*) AS n FROM model.events e WHERE {w} GROUP BY 1 ORDER BY 2 DESC",
                           p).fetchall()
    return {"total": total, "limit": _limit(limit), "offset": offset,
            "by_type": {r["type"]: r["n"] for r in by_type}, "events": rows}


# --- jurisdictions and coverage --------------------------------------------------------------

def jurisdiction(conn: Connection, code: str, *, today: date, recent: int = 20) -> dict[str, Any]:
    regs = jurisdictions.resolve(code)
    code = jurisdictions.normalise(code)
    slugs = _slugs(regs)
    counts = {
        "parties": _count(conn, "parties", slugs),
        "licences": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM model.licences WHERE register = ANY(%s) AND current "
            "GROUP BY 1 ORDER BY 2 DESC", (slugs,))},
        "brands": _count(conn, "brands", slugs),
        "websites": _count(conn, "domains", slugs),
        "blocked_domains": _count(conn, "blocks", slugs),
    }
    products = conn.execute(
        "SELECT p AS product, count(*) AS n FROM model.licences, unnest(products) p "
        "WHERE register = ANY(%s) AND current GROUP BY 1 ORDER BY 2 DESC", (slugs,)).fetchall()
    month = conn.execute(
        "SELECT type, count(*) AS n FROM model.events WHERE register = ANY(%s) AND at > now() - interval '30 days' "
        "GROUP BY 1 ORDER BY 2 DESC", (slugs,)).fetchall()
    latest = conn.execute("SELECT * FROM model.events WHERE register = ANY(%s) ORDER BY at DESC NULLS LAST, id "
                          "LIMIT %s", (slugs, recent)).fetchall()
    src = sources(conn, set(slugs))
    return {
        "code": code, "name": jurisdictions.name(code),
        "registers": [{"slug": r.slug, "regulator": r.regulator, "name": r.name, "kind": r.kind,
                       "homepage": r.homepage, "covers": sorted(catalogue.coverage(r.slug).covers),
                       "scope": catalogue.coverage(r.slug).scope, "cadence": catalogue.coverage(r.slug).cadence,
                       "last_good": src[r.slug]["last_good"]} for r in regs],
        "counts": counts, "products": {r["product"]: r["n"] for r in products},
        "last_30_days": {r["type"]: r["n"] for r in month}, "recent_events": latest,
        "pending_regulator": next((vars(u) for u in catalogue.PENDING if u.code == code), None),
    }


def _count(conn: Connection, table: str, slugs: list[str]) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM model.{table} WHERE register = ANY(%s) AND current",
                        (slugs,)).fetchone()["n"]


def coverage(conn: Connection) -> dict[str, Any]:
    """What we hold, how fresh, how complete — and what we do not hold.

    quality is per register, over current rows: the share of parties with an
    identifier beyond their name, of licences with a stated status / end date /
    reference, of websites and blocks whose text parsed to a hostname."""
    quality: dict[str, dict[str, Any]] = defaultdict(dict)
    for r in conn.execute(
            "SELECT register, count(*) AS n, avg((identifiers <> '{}'::jsonb)::int) AS with_identifier "
            "FROM model.parties WHERE current GROUP BY 1"):
        quality[r["register"]]["parties"] = {"n": r["n"], "with_identifier": _pct(r["with_identifier"])}
    for r in conn.execute(
            "SELECT register, count(*) AS n, avg((status <> 'listed')::int) AS with_status, "
            "avg((valid_to IS NOT NULL)::int) AS with_end_date, avg((reference IS NOT NULL)::int) AS with_reference, "
            "avg((party_id IS NOT NULL)::int) AS linked FROM model.licences WHERE current GROUP BY 1"):
        quality[r["register"]]["licences"] = {"n": r["n"], "with_status": _pct(r["with_status"]),
                                              "with_end_date": _pct(r["with_end_date"]),
                                              "with_reference": _pct(r["with_reference"]),
                                              "linked_to_party": _pct(r["linked"])}
    for table, key in (("domains", "websites"), ("blocks", "blocked_domains")):
        for r in conn.execute(f"SELECT register, count(*) AS n, avg((host IS NOT NULL)::int) AS parsed "
                              f"FROM model.{table} WHERE current GROUP BY 1"):
            quality[r["register"]][key] = {"n": r["n"], "hostname_parsed": _pct(r["parsed"])}
    for r in conn.execute("SELECT register, count(*) AS n FROM model.brands WHERE current GROUP BY 1"):
        quality[r["register"]]["brands"] = {"n": r["n"]}
    src = sources(conn, set(REGISTRY))
    regs = []
    for slug, c in sorted(catalogue.COVERAGE.items()):
        regs.append({"slug": slug, **src[slug], "kind": REGISTRY[slug].kind, "covers": sorted(c.covers),
                     "scope": c.scope, "cadence": c.cadence, "quality": quality.get(slug, {})})
    matrix = []
    for code, rs in jurisdictions.by_code().items():
        cov = set().union(*(catalogue.coverage(r.slug).covers for r in rs))
        matrix.append({"jurisdiction": code, "name": jurisdictions.name(code),
                       **{k: k in cov for k in ("party", "licence", "brand", "domain", "block")}})
    return {"model": model_repo.freshness(conn), "registers": regs, "matrix": matrix,
            "not_covered": [vars(u) for u in catalogue.UNCOVERED],
            "pending": [vars(u) for u in catalogue.PENDING]}


def _pct(v: Any) -> float | None:
    return None if v is None else round(float(v) * 100, 1)
