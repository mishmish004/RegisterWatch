-- 0008 — ingest runs as records, not process memory (plan.md Phase 5).
--
-- One row per batch: what was asked, who is running it, how it ended. One
-- result row per register it ran. Survives restarts and is the same for every
-- replica, which `/ingest/last` (process memory) never was.
--
-- Idempotency: a client may send `Idempotency-Key`; the key and a hash of the
-- request body are stored so a replay returns the original run and a reuse with
-- a different body is refused. Keys are honoured for 24 hours (the API clears
-- older ones before it looks), so the column is unique only while it is set.
--
-- Plain Postgres; RLS on and default grants revoked as in 0006, so Supabase's
-- PostgREST never serves these to the anon key.

CREATE TABLE IF NOT EXISTS ingest_runs (
  id              uuid PRIMARY KEY,
  status          text NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'succeeded', 'partial', 'failed')),
  requested       jsonb NOT NULL,
  registers       text[] NOT NULL,
  not_started     text[] NOT NULL DEFAULT '{}',
  idempotency_key text UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 255),
  request_hash    bytea,
  created_at      timestamptz NOT NULL DEFAULT now(),
  started_at      timestamptz,
  finished_at     timestamptz,
  error           text,
  CONSTRAINT key_has_hash      CHECK (idempotency_key IS NULL OR request_hash IS NOT NULL),
  CONSTRAINT finished_is_final CHECK ((finished_at IS NULL) = (status IN ('queued', 'running')))
);

CREATE INDEX IF NOT EXISTS ingest_runs_newest ON ingest_runs (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ingest_runs_active ON ingest_runs (created_at DESC)
  WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS ingest_run_results (
  run_id        uuid NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
  slug          text NOT NULL,
  position      int  NOT NULL,
  snapshot_id   bigint REFERENCES raw_snapshots(id),
  complete      boolean NOT NULL,
  skipped       boolean NOT NULL DEFAULT false,
  unchanged     boolean NOT NULL DEFAULT false,
  reason        text,
  record_count  int,
  finished_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, slug)
);

ALTER TABLE ingest_runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_run_results ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON ingest_runs, ingest_run_results FROM %I', r);
    END IF;
  END LOOP;
END $$;
