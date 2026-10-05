-- 0006 — keep the evidence tables out of Supabase's public API.
--
-- Supabase serves the `public` schema over PostgREST to anyone holding the anon
-- key, and grants anon/authenticated on new public tables by default. These
-- tables are written by the ingest (connecting as postgres, which bypasses RLS)
-- and read by nobody else: RLS on with no policies, and the default grants
-- revoked. The register schemas are not exposed to PostgREST at all.
--
-- Guarded so the same file runs on plain Postgres, where those roles don't exist.

ALTER TABLE sources       ENABLE ROW LEVEL SECURITY;
ALTER TABLE raw_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE host_state    ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON sources, raw_snapshots, host_state FROM %I', r);
      EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', r);
    END IF;
  END LOOP;
END $$;
