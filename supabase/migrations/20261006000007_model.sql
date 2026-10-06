-- 0007 — the model: what the registers say, as one set of tables.
--
-- Derived data. `registerwatch build`, and every ingest batch that recorded a
-- complete snapshot, rebuilds all of it from the register schemas in one
-- transaction (src/registerwatch/model/). Nothing else writes here, and
-- dropping the schema loses nothing a build cannot recreate: the evidence stays
-- in the register schemas and raw_snapshots, and every row below names the
-- register rows behind it in `evidence`.
--
--   parties   a legal entity as one register names it; cluster_id groups the
--             same name across registers (an "operator")
--   licences  permissions, with status normalised beside the published one
--   brands    trading names
--   domains   websites a licensee register lists (one row per listing)
--   blocks    blocklist entries
--   events    changes derived from the row history: <kind>.added, .removed,
--             .status_changed, .changed
--
-- No foreign keys between them: they are rebuilt together, and a register can
-- name a party in one table that its other table does not carry.

CREATE SCHEMA IF NOT EXISTS model;

CREATE TABLE IF NOT EXISTS model.builds (
  id             bigserial PRIMARY KEY,
  started_at     timestamptz NOT NULL,
  finished_at    timestamptz NOT NULL DEFAULT now(),
  model_version  int NOT NULL,
  inputs         jsonb NOT NULL,   -- per register: newest snapshot read, rows read
  counts         jsonb NOT NULL    -- per register: model rows by kind
);

CREATE TABLE IF NOT EXISTS model.parties (
  party_id       text PRIMARY KEY,
  register       text NOT NULL,
  jurisdiction   text NOT NULL,
  regulator      text NOT NULL,
  name           text,
  name_key       text,
  name_core      text,
  cluster_id     text,
  identifiers    jsonb NOT NULL DEFAULT '{}',
  aliases        text[] NOT NULL DEFAULT '{}',
  current        boolean NOT NULL,
  first_seen_at  timestamptz,
  last_seen_at   timestamptz,
  removed_at     timestamptz,
  versions       int NOT NULL,
  evidence       jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS parties_cluster ON model.parties (cluster_id);
CREATE INDEX IF NOT EXISTS parties_jurisdiction ON model.parties (jurisdiction);

CREATE TABLE IF NOT EXISTS model.licences (
  licence_id     text PRIMARY KEY,
  register       text NOT NULL,
  jurisdiction   text NOT NULL,
  party_id       text,
  regulator      text NOT NULL,
  authority      text,
  reference      text,
  type           text,
  products       text[] NOT NULL DEFAULT '{}',
  channel        text,
  area           text,
  site           text,
  status         text NOT NULL,
  status_raw     text,
  status_since   date,
  valid_from     date,
  valid_to       date,
  notes          text,
  current        boolean NOT NULL,
  first_seen_at  timestamptz,
  last_seen_at   timestamptz,
  removed_at     timestamptz,
  versions       int NOT NULL,
  evidence       jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS licences_party ON model.licences (party_id);
CREATE INDEX IF NOT EXISTS licences_jurisdiction_status ON model.licences (jurisdiction, status);

CREATE TABLE IF NOT EXISTS model.brands (
  brand_id       text PRIMARY KEY,
  register       text NOT NULL,
  jurisdiction   text NOT NULL,
  party_id       text,
  name           text,
  name_key       text,
  status         text NOT NULL,
  status_raw     text,
  products       text[] NOT NULL DEFAULT '{}',
  current        boolean NOT NULL,
  first_seen_at  timestamptz,
  last_seen_at   timestamptz,
  removed_at     timestamptz,
  versions       int NOT NULL,
  evidence       jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS brands_party ON model.brands (party_id);
CREATE INDEX IF NOT EXISTS brands_name_key ON model.brands (name_key);

CREATE TABLE IF NOT EXISTS model.domains (
  listing_id     text PRIMARY KEY,
  register       text NOT NULL,
  jurisdiction   text NOT NULL,
  host           text,
  registrable    text,
  label          text,
  published      text,
  party_id       text,
  brand_id       text,
  licence_id     text,
  status         text NOT NULL,
  status_raw     text,
  products       text[] NOT NULL DEFAULT '{}',
  since          date,
  current        boolean NOT NULL,
  first_seen_at  timestamptz,
  last_seen_at   timestamptz,
  removed_at     timestamptz,
  versions       int NOT NULL,
  evidence       jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS domains_host ON model.domains (host);
CREATE INDEX IF NOT EXISTS domains_registrable ON model.domains (registrable);
CREATE INDEX IF NOT EXISTS domains_label ON model.domains (label);
CREATE INDEX IF NOT EXISTS domains_party ON model.domains (party_id);

CREATE TABLE IF NOT EXISTS model.blocks (
  block_id       text PRIMARY KEY,
  register       text NOT NULL,
  jurisdiction   text NOT NULL,
  regulator      text NOT NULL,
  host           text,
  registrable    text,
  label          text,
  published      text,
  listed_on      date,
  current        boolean NOT NULL,
  first_seen_at  timestamptz,
  last_seen_at   timestamptz,
  removed_at     timestamptz,
  versions       int NOT NULL,
  evidence       jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS blocks_host ON model.blocks (host);
CREATE INDEX IF NOT EXISTS blocks_registrable ON model.blocks (registrable);
CREATE INDEX IF NOT EXISTS blocks_label ON model.blocks (label);

CREATE TABLE IF NOT EXISTS model.events (
  id             text PRIMARY KEY,   -- stable across rebuilds
  at             timestamptz,
  snapshot_id    bigint NOT NULL,
  register       text NOT NULL,
  jurisdiction   text NOT NULL,
  kind           text NOT NULL,
  type           text NOT NULL,
  subject_id     text NOT NULL,
  party_id       text,
  host           text,
  before         jsonb,
  after          jsonb,
  summary        text NOT NULL
);
CREATE INDEX IF NOT EXISTS events_at ON model.events (at DESC);
CREATE INDEX IF NOT EXISTS events_jurisdiction_at ON model.events (jurisdiction, at DESC);
CREATE INDEX IF NOT EXISTS events_subject ON model.events (subject_id);
CREATE INDEX IF NOT EXISTS events_party ON model.events (party_id);
CREATE INDEX IF NOT EXISTS events_host ON model.events (host);

-- Like the register schemas, not for Supabase's public API: the registerwatch
-- API serves it. Guarded so the file runs on plain Postgres.
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON SCHEMA model FROM %I', r);
      EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA model FROM %I', r);
    END IF;
  END LOOP;
END $$;
