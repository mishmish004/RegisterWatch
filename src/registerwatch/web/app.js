"use strict";
// RegisterWatch's front end: the API's model endpoints, rendered. No build step,
// no dependencies. Everything shown comes from a regulator's register, so every
// value goes through esc() and every link through safeUrl() before it is drawn.

const view = () => document.getElementById("view");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
// Dates keep their hyphens unbreakable so a narrow cell does not split 2026-10-06.
const day = (v) => (v ? esc(String(v).slice(0, 10)).replace(/-/g, "\u2011") : "—");
const enc = encodeURIComponent;
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: ask again next time */ } },
};

async function api(path) {
  const headers = {};
  const token = store.get("rw_read_token");
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(path, { headers });
  if (r.status === 401) {
    const t = window.prompt("This RegisterWatch API needs a read token (READ_TOKEN):");
    if (t) { store.set("rw_read_token", t.trim()); return api(path); }
    throw new Error("A read token is required.");
  }
  if (!r.ok) {
    let detail;
    try { detail = (await r.json()).detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : `The API answered ${r.status}.`);
  }
  return r.json();
}

// --- small renderers ---------------------------------------------------------------

const VERDICT_ORDER = ["conflict", "blocked", "listed_not_operating", "authorised", "blocked_parent", "related_listed",
  "previously_listed", "previously_blocked", "not_listed", "not_blocked", "no_domain_data"];
const QUIET = new Set(["not_listed", "not_blocked", "no_domain_data"]);
const STATUS_CLASS = {
  active: "v-authorised", listed: "v-authorised", white_label: "v-authorised",
  suspended: "v-blocked", revoked: "v-blocked", forfeited: "v-blocked",
  surrendered: "v-previously_listed", expired: "v-previously_listed", lapsed: "v-previously_listed",
  pending: "v-related_listed", inactive: "v-listed_not_operating", unknown: "v-no_domain_data",
};

const chip = (text, cls = "") => `<span class="chip ${esc(cls)}">${esc(text)}</span>`;
const statusChip = (status, raw) => chip(raw || status, STATUS_CLASS[status] || "v-no_domain_data");
const jurLink = (code) => `<a href="#/jurisdiction/${enc(code.toLowerCase())}">${esc(code)}</a>`;
const opLink = (id, name) => (id ? `<a href="#/operator/${enc(id)}">${esc(name || id)}</a>` : esc(name || ""));
const hostLink = (h) => (h ? `<a href="#/domain/${enc(h)}">${esc(h)}</a>` : "");

function table(cols, rows) {
  if (!rows.length) return `<p class="muted">None.</p>`;
  return `<div class="table-wrap"><table><thead><tr>${cols.map((c) => `<th>${esc(c[0])}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td>${c[1](r)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function evidence(m, sources) {
  const src = sources?.[m.register] || {};
  const rows = (m.evidence || []).slice(0, 3).map((e) => `${esc(e.table)} row ${esc(e.row_id)}`).join(", ");
  const link = safeUrl(src.homepage);
  return `<div class="evidence">Source: ${link ? `<a href="${esc(link)}" rel="noopener noreferrer" target="_blank">${esc(src.regulator || m.register)}</a>` : esc(m.register)}
    · <code>${esc(m.register)}</code>${rows ? ` · ${rows}` : ""} · on our copy since ${day(m.first_seen_at)}
    ${m.removed_at ? ` · removed ${day(m.removed_at)}` : ""}${src.last_good ? ` · register last read ${day(src.last_good)}` : ""}</div>`;
}

function timeline(events) {
  if (!events.length) return `<p class="muted">No changes recorded since watching began.</p>`;
  return `<ul class="timeline">${events.map((e) => `<li><span class="when">${day(e.at)}</span>
    <span>${esc(e.summary)} <span class="type">${esc(e.type)}</span></span></li>`).join("")}</ul>`;
}

function loading(what) { view().innerHTML = `<div class="empty">Loading ${esc(what)}…</div>`; }
function failed(err) { view().innerHTML = `<div class="error">${esc(err.message || err)}</div>`; }

// --- views ---------------------------------------------------------------------------

async function viewHome() {
  view().innerHTML = `<section class="hero"><h1>Is this site allowed, and who runs it?</h1>
    <p>One answer per jurisdiction from ${"<a href='#/coverage'>21 regulators' own registers</a>"}: authorised,
    blocked, listed but not operating, related host listed, previously listed, not listed, or no data — with the
    register rows behind each answer and what changed since.</p>
    <div class="chips">Try ${["bet365.com", "fastbet.com", "casino777.be"].map((d) => `<a class="chip v-no_domain_data" href="#/domain/${enc(d)}">${esc(d)}</a>`).join("")}
    ${["Hillside", "Betway"].map((q) => `<a class="chip v-no_domain_data" href="#/operators/${enc(q)}">${esc(q)}</a>`).join("")}</div></section>
    <h2>Changes in the last 7 days</h2><div id="recent" class="muted">Loading…</div>`;
  try {
    const r = await api("/events?limit=15");
    document.getElementById("recent").outerHTML = `<div>${timeline(r.events)}
      <p><a href="#/events">All changes →</a></p></div>`;
  } catch (e) {
    document.getElementById("recent").textContent = e.message;
  }
}

async function viewDomain(value) {
  loading(value);
  const r = await api(`/domains/${enc(value)}`);
  const order = (v) => VERDICT_ORDER.indexOf(v.verdict);
  const verdicts = [...r.verdicts].sort((a, b) => order(a) - order(b) || a.jurisdiction.localeCompare(b.jurisdiction));
  const loud = verdicts.filter((v) => !QUIET.has(v.verdict));
  const quiet = verdicts.filter((v) => QUIET.has(v.verdict));
  const list = (label, codes, cls) => (codes.length ? `${esc(label)} ${codes.map((c) => chip(c, cls)).join("")}` : "");
  view().innerHTML = `<h1 class="mono">${esc(r.domain)}</h1>
    <div class="muted small">registrable domain <code>${esc(r.registrable || "—")}</code></div>
    <div class="chips">${list("Authorised in", r.authorised_in, "v-authorised")} ${list("Blocked in", r.blocked_in, "v-blocked")}
      ${list("Look closer at", r.attention, "v-related_listed")}
      ${!r.authorised_in.length && !r.blocked_in.length ? chip("Not named by any register held", "v-no_domain_data") : ""}</div>
    ${loud.length ? `<div class="grid">${loud.map((v) => verdictCard(v, r.sources)).join("")}</div>` : ""}
    ${quiet.length ? `<details><summary>${quiet.length} jurisdiction(s) that do not name it</summary>
      ${table([["Jurisdiction", (v) => jurLink(v.jurisdiction)], ["Answer", (v) => chip(v.label, `v-${v.verdict}`)],
        ["Why", (v) => esc(v.explanation)], ["Caveat", (v) => esc(v.caveats[0] || "")]], quiet)}</details>` : ""}
    <h2>Same name elsewhere</h2>
    <p class="muted small">Other domains with the label <code>${esc(r.label || "—")}</code>. A lead to check, not a link the registers make.</p>
    ${table([["Jurisdiction", (x) => jurLink(x.jurisdiction)], ["", (x) => chip(x.kind === "block" ? "blocked" : "listed", x.kind === "block" ? "v-blocked" : "v-authorised")],
      ["Host", (x) => hostLink(x.host)], ["Listed for", (x) => opLink(x.operator_id, x.party)],
      ["Now", (x) => (x.current ? "yes" : "removed")]], r.same_name_elsewhere)}
    <h2>History</h2>${timeline(r.history)}`;
}

function verdictCard(v, sources) {
  const matches = v.matches.filter((m) => m.relation === "exact" || !QUIET.has(v.verdict)).slice(0, 6);
  return `<article class="card"><div class="card-head"><h3>${jurLink(v.jurisdiction)} · ${esc(v.name)}</h3>
      <span>${chip(v.label, `v-${v.verdict}`)} <span class="conf">${esc(v.confidence)} confidence</span></span></div>
    <p>${esc(v.explanation)}</p>
    ${v.caveats.length ? `<ul class="caveat">${v.caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
    ${matches.map((m) => matchBlock(m, sources)).join("")}</article>`;
}

function matchBlock(m, sources) {
  const rel = m.relation === "exact" ? "" : ` <span class="muted">(${esc(m.relation)} host)</span>`;
  if (m.kind === "block") {
    return `<div class="match">${chip(m.current ? "on blocklist" : "was on blocklist", m.current ? "v-blocked" : "v-previously_blocked")}
      <code>${esc(m.published)}</code>${rel}${m.listed_on ? ` · listed ${day(m.listed_on)}` : ""}${evidence(m, sources)}</div>`;
  }
  const lic = m.licence;
  const nodes = [
    m.party ? `<span class="node">${opLink(m.party.operator_id, m.party.name)}</span>` : "",
    lic ? `<span class="node">${esc(lic.reference || lic.type || "licence")} ${statusChip(lic.status, lic.status_raw)}</span>` : "",
    m.brand ? `<span class="node">${esc(m.brand)}</span>` : "",
    `<span class="node mono">${esc(m.published)}</span>`,
  ].filter(Boolean);
  return `<div class="match"><div class="chain">${nodes.join('<span class="arrow">→</span>')}${rel}</div>
    <div class="small">listing ${statusChip(m.status, m.status_raw)}${m.current ? "" : " · removed"}
    ${m.products?.length ? ` · ${esc(m.products.join(", "))}` : ""}${m.since ? ` · since ${day(m.since)}` : ""}
    ${lic?.valid_to ? ` · licence to ${day(lic.valid_to)}` : ""}${lic?.authority && lic.authority !== lic.regulator ? ` · issued by ${esc(lic.authority)}` : ""}</div>
    ${evidence(m, sources)}</div>`;
}

async function viewOperatorSearch(q) {
  loading(q);
  const r = await api(`/operators?q=${enc(q)}`);
  view().innerHTML = `<h1>Operators matching “${esc(q)}”</h1>
    <p class="muted small">Matched on company name, trading name or website. One operator is the same legal name across registers.</p>
    ${table([["Operator", (o) => opLink(o.operator_id, o.name)], ["Jurisdictions", (o) => o.jurisdictions.map(jurLink).join(" ")],
      ["Matched on", (o) => esc(o.matched_on.join(", "))], ["Listed now", (o) => (o.current ? "yes" : "no")]], r.operators)}
    ${r.brands_without_operator.length ? `<h2>Brands listed without a company</h2>
      ${table([["Brand", (b) => esc(b.name)], ["Jurisdiction", (b) => jurLink(b.jurisdiction)],
        ["Products", (b) => esc((b.products || []).join(", "))]], r.brands_without_operator)}` : ""}`;
}

async function viewOperator(id) {
  loading(id);
  const r = await api(`/operators/${enc(id)}`);
  const byJur = {};
  for (const p of r.parties) (byJur[p.jurisdiction] ||= { parties: [], licences: [], websites: [], brands: [] }).parties.push(p);
  for (const l of r.licences) byJur[l.jurisdiction]?.licences.push(l);
  for (const d of r.websites) byJur[d.jurisdiction]?.websites.push(d);
  for (const b of r.brands) byJur[b.jurisdiction]?.brands.push(b);
  view().innerHTML = `<h1>${esc(r.name)}</h1><div class="muted small"><code>${esc(r.operator_id)}</code></div>
    ${r.flags.length ? `<h2>Look closer</h2>${r.flags.map((f) => `<div class="flag">⚠ ${esc(f.text)}</div>`).join("")}` : ""}
    <h2>Footprint</h2>
    ${table([["Jurisdiction", (f) => jurLink(f.jurisdiction)], ["Listed now", (f) => (f.listed ? "yes" : "no")],
      ["Licences", (f) => Object.entries(f.licences).map(([s, n]) => statusChip(s, `${n} ${s}`)).join(" ") || "—"],
      ["Products", (f) => esc(f.products.join(", ") || "—")], ["Websites", (f) => esc(f.websites)]], r.footprint)}
    <h2>Entity → licence → website, per jurisdiction</h2>
    <div class="grid">${Object.entries(byJur).map(([code, j]) => `<article class="card">
      <div class="card-head"><h3>${jurLink(code)}</h3><span class="muted small">${esc(r.sources[j.parties[0].register]?.regulator || "")}</span></div>
      ${j.parties.map((p) => `<div class="chain"><span class="node">${esc(p.name)}</span>${p.current ? "" : chip("removed", "v-previously_listed")}
        ${Object.entries(p.identifiers || {}).map(([k, v]) => `<span class="muted small">${esc(k)} ${esc(v)}</span>`).join(" ")}</div>`).join("")}
      ${j.licences.slice(0, 12).map((l) => `<div class="chain small"><span class="arrow">→</span>
        <span class="node">${esc(l.reference || l.type || "licence")}${l.reference && l.type ? ` · ${esc(l.type)}` : ""}</span>
        ${statusChip(l.status, l.status_raw)}${l.valid_to ? ` <span class="muted">to ${day(l.valid_to)}</span>` : ""}</div>`).join("")}
      ${j.licences.length > 12 ? `<div class="muted small">… ${j.licences.length - 12} more</div>` : ""}
      ${j.brands.length ? `<div class="small muted">Trading names: ${j.brands.map((b) => esc(b.name)).join(", ")}</div>` : ""}
      ${j.websites.length ? `<div class="small">Websites: ${[...new Set(j.websites.filter((d) => d.current).map((d) => d.host).filter(Boolean))].map(hostLink).join(", ") || "—"}</div>` : ""}
      ${evidence({ register: j.parties[0].register, evidence: j.parties[0].evidence, first_seen_at: j.parties[0].first_seen_at }, r.sources)}
      </article>`).join("")}</div>
    <h2>Its domains on blocklists</h2>
    ${table([["Jurisdiction", (b) => jurLink(b.jurisdiction)], ["Host", (b) => hostLink(b.host)],
      ["Blocklist", (b) => esc(r.sources[b.register]?.regulator || b.register)],
      ["Listed", (b) => day(b.listed_on)], ["Now", (b) => (b.current ? chip("blocked", "v-blocked") : "removed")]], r.websites_blocked)}
    ${r.related_operators.length ? `<h2>Other operators listing the same domains</h2>
      ${table([["Operator", (o) => opLink(o.operator_id, o.name)], ["Shared", (o) => o.shared_domains.map(hostLink).join(", ")],
        ["Where", (o) => o.jurisdictions.map(jurLink).join(" ")]], r.related_operators)}` : ""}
    <h2>History</h2>${timeline(r.history)}`;
}

const EVENT_TYPES = ["", "licence.added", "licence.removed", "licence.status_changed", "licence.changed",
  "domain.added", "domain.removed", "domain.status_changed", "block.added", "block.removed",
  "party.added", "party.removed", "party.changed", "brand.added", "brand.removed"];

async function viewEvents(params) {
  loading("changes");
  const since = params.get("since") || "";
  const jur = params.get("jurisdiction") || "";
  const type = params.get("type") || "";
  const qs = new URLSearchParams({ limit: "200" });
  if (since) qs.set("since", since);
  if (jur) qs.set("jurisdiction", jur);
  if (type) qs.set("type", type);
  const r = await api(`/events?${qs}`);
  view().innerHTML = `<h1>What changed</h1>
    <form class="filters" id="ef">
      <input name="since" type="date" value="${esc(since || String(r.since).slice(0, 10))}" aria-label="Since">
      <input name="jurisdiction" placeholder="Jurisdictions, e.g. gb,se" value="${esc(jur)}" aria-label="Jurisdictions">
      <select name="type" aria-label="Type">${EVENT_TYPES.map((t) => `<option value="${esc(t)}"${t === type ? " selected" : ""}>${esc(t || "every type")}</option>`).join("")}</select>
      <button type="submit">Show</button></form>
    <div class="chips">${Object.entries(r.by_type).map(([t, n]) => chip(`${t} ${n}`, "v-no_domain_data")).join("")}</div>
    <p class="muted small">${esc(r.total)} change(s) since ${day(r.since)}; the first snapshot of each register is its baseline, not news.</p>
    ${timeline(r.events)}`;
  document.getElementById("ef").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const p = new URLSearchParams([...f].filter(([, v]) => v));
    location.hash = `#/events?${p}`;
  });
}

async function viewJurisdiction(code) {
  loading(code);
  const r = await api(`/jurisdictions/${enc(code)}/profile`);
  view().innerHTML = `<h1>${esc(r.code)} — ${esc(r.name)}</h1>
    ${r.registers.map((g) => `<div class="panel stack">
      <h3>${safeUrl(g.homepage) ? `<a href="${esc(g.homepage)}" target="_blank" rel="noopener noreferrer">${esc(g.regulator)}</a>` : esc(g.regulator)}</h3>
      <p>${esc(g.scope)}</p><div class="small muted">covers ${esc(g.covers.join(", "))}${g.cadence ? ` · publishes ${esc(g.cadence)}` : ""}
      · last read ${day(g.last_good)} · <code>${esc(g.slug)}</code></div></div>`).join("")}
    ${r.pending_regulator ? `<p class="flag">⚠ ${esc(r.pending_regulator.regulator)}: ${esc(r.pending_regulator.reason)}</p>` : ""}
    <h2>Now</h2><div class="chips">${chip(`${r.counts.parties} parties`, "v-no_domain_data")} ${chip(`${r.counts.brands} trading names`, "v-no_domain_data")}
      ${chip(`${r.counts.websites} listed websites`, "v-no_domain_data")} ${chip(`${r.counts.blocked_domains} blocked domains`, "v-no_domain_data")}</div>
    <div class="chips">${Object.entries(r.counts.licences).map(([s, n]) => statusChip(s, `${n} ${s}`)).join("")}</div>
    ${Object.keys(r.products).length ? `<div class="chips">Licensed products: ${Object.entries(r.products).map(([p, n]) => chip(`${p} ${n}`)).join("")}</div>` : ""}
    <h2>Recent changes</h2>${timeline(r.recent_events)}
    <p><a href="#/events?jurisdiction=${enc(r.code.toLowerCase())}">All changes in ${esc(r.code)} →</a></p>`;
}

async function viewCoverage() {
  loading("coverage");
  const r = await api("/coverage");
  const yes = (b) => (b ? "✓" : "");
  const q = (x, k) => x.quality?.[k]?.n ?? "";
  view().innerHTML = `<h1>Coverage</h1>
    <p class="muted">Model build ${esc(r.model.build_id ?? "—")} at ${esc(day(r.model.built_at))}${r.model.behind ? " — behind the newest snapshot" : ""}.
    What each jurisdiction's registers let us answer:</p>
    ${table([["Jurisdiction", (m) => `${jurLink(m.jurisdiction)} ${esc(m.name)}`], ["Entities", (m) => yes(m.party)],
      ["Licences", (m) => yes(m.licence)], ["Trading names", (m) => yes(m.brand)], ["Websites", (m) => yes(m.domain)],
      ["Blocklist", (m) => yes(m.block)]], r.matrix)}
    <h2>Registers</h2>
    ${table([["Register", (x) => `<code>${esc(x.slug)}</code>`],
      ["Regulator", (x) => (safeUrl(x.homepage) ? `<a href="${esc(x.homepage)}" target="_blank" rel="noopener noreferrer">${esc(x.regulator)}</a>` : esc(x.regulator))],
      ["Last read", (x) => day(x.last_good)], ["Parties", (x) => esc(q(x, "parties"))], ["Licences", (x) => esc(q(x, "licences"))],
      ["Websites", (x) => esc(q(x, "websites"))], ["Blocked", (x) => esc(q(x, "blocked_domains"))],
      ["Licences with status / end date", (x) => (x.quality?.licences ? `${esc(x.quality.licences.with_status)}% / ${esc(x.quality.licences.with_end_date)}%` : "")]], r.registers)}
    <h2>Checked, no usable register</h2>
    <p class="muted small">An answer about these jurisdictions is “no data”, not silence.</p>
    ${table([["Code", (u) => esc(u.code)], ["Regulator", (u) => esc(u.regulator)], ["Why", (u) => esc(u.reason)]], [...r.not_covered, ...r.pending])}`;
}

// --- routing ---------------------------------------------------------------------------

const ROUTES = [
  [/^#\/domain\/(.+)$/, (m) => viewDomain(decodeURIComponent(m[1]))],
  [/^#\/operators\/(.+)$/, (m) => viewOperatorSearch(decodeURIComponent(m[1]))],
  [/^#\/operator\/(.+)$/, (m) => viewOperator(decodeURIComponent(m[1]))],
  [/^#\/events(?:\?(.*))?$/, (m) => viewEvents(new URLSearchParams(m[1] || ""))],
  [/^#\/jurisdiction\/(.+)$/, (m) => viewJurisdiction(decodeURIComponent(m[1]))],
  [/^#\/coverage$/, () => viewCoverage()],
];

async function route() {
  const hash = location.hash || "#/";
  const hit = ROUTES.find(([re]) => re.test(hash));
  try {
    await (hit ? hit[1](hash.match(hit[0])) : viewHome());
  } catch (e) {
    failed(e);
  }
  window.scrollTo(0, 0);
}

document.getElementById("search").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = document.getElementById("q").value.trim();
  if (!q) return;
  // Something with a dot and no spaces is a website; anything else, a name.
  location.hash = /^\S+\.[a-z][a-z0-9-]+(\/\S*)?$/i.test(q) ? `#/domain/${enc(q)}` : `#/operators/${enc(q)}`;
});
window.addEventListener("hashchange", route);
route();
