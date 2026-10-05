-- 001_init.sql — step 1 only: fetch + store + hash.
-- observations / entities / licences / transitions arrive in 002, 003.

CREATE TABLE IF NOT EXISTS sources (
  id             serial PRIMARY KEY,
  slug           text NOT NULL UNIQUE,
  display_name   text NOT NULL,
  jurisdiction   text NOT NULL,
  config         jsonb NOT NULL DEFAULT '{}',
  enabled        boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT slug_shape CHECK (slug ~ '^[a-z0-9_]+$')
);

-- Evidence layer. Immutable by grant, not by convention (see bottom of file).
CREATE TABLE IF NOT EXISTS raw_snapshots (
  id                bigserial PRIMARY KEY,
  source_id         int NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
  run_started_at    timestamptz NOT NULL,
  fetched_at        timestamptz NOT NULL DEFAULT now(),

  -- raw_hash: over the concatenated per-page content hashes, in ordinal order.
  -- canonical_hash: over the normalized observation set. NULL until parse (step 2).
  raw_hash          bytea NOT NULL,
  canonical_hash    bytea,

  blob_ref          text NOT NULL,          -- manifest object path
  record_count      int,                    -- NULL until parse
  http_status       int NOT NULL,           -- worst final status across pages

  pages_expected    int NOT NULL,
  pages_ok          int NOT NULL,
  fetch_log         jsonb NOT NULL DEFAULT '[]',

  complete          boolean NOT NULL DEFAULT false,
  incomplete_reason text,
  parsed_at         timestamptz,

  CONSTRAINT complete_needs_count   CHECK (NOT complete OR record_count IS NOT NULL),
  CONSTRAINT complete_needs_pages   CHECK (NOT complete OR pages_ok >= pages_expected),
  CONSTRAINT incomplete_has_reason  CHECK (complete OR incomplete_reason IS NOT NULL),
  CONSTRAINT not_future             CHECK (fetched_at <= now() + interval '1 minute'),
  CONSTRAINT pages_sane             CHECK (pages_ok >= 0 AND pages_expected >= 0)
);

-- Deliberately NOT unique on raw_hash: an unchanged register produces the same
-- hash daily and the fetch still has to be recorded.
CREATE INDEX IF NOT EXISTS snapshots_by_source_time
  ON raw_snapshots (source_id, fetched_at DESC);

-- The differ's input query is `WHERE complete` — index it.
CREATE INDEX IF NOT EXISTS snapshots_complete
  ON raw_snapshots (source_id, fetched_at DESC) WHERE complete;

CREATE INDEX IF NOT EXISTS snapshots_unparsed
  ON raw_snapshots (source_id, fetched_at) WHERE parsed_at IS NULL;

-- Rate-limit reservation state. Keyed by HOST, not source: two registers behind
-- one government CDN share a budget, and a per-source limiter doubles the load
-- on them.  Infrastructure, not domain — this is not one of "the six".
CREATE TABLE IF NOT EXISTS host_state (
  host              text PRIMARY KEY,
  next_slot_at      timestamptz NOT NULL DEFAULT now(),
  interval_ms       int NOT NULL,
  floor_interval_ms int NOT NULL,
  consecutive_429   int NOT NULL DEFAULT 0,
  last_429_at       timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT interval_sane CHECK (interval_ms >= floor_interval_ms AND interval_ms > 0)
);

-- Immutability. Run as the owner, then let the app connect as app_user.
--   REVOKE UPDATE, DELETE ON raw_snapshots FROM app_user;
-- Left commented because it depends on your Supabase role names; do it before
-- the first customer, not after.
