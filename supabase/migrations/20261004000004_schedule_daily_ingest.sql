-- 0004 — the daily ingest of every register, scheduled from inside Supabase.
--
-- Supabase cannot run Python, so the schedule lives here and the work lives in
-- the registerwatch API: pg_cron fires, pg_net POSTs /ingest/all, the API
-- answers 202 at once and runs all 21 registers in the background (a few
-- minutes: each host is rate-limited to one request per few seconds). Every
-- register writes its own raw_snapshots row, success or not; those rows are the
-- record, not the HTTP response.
--
-- Three slots, one run:
--   06:00 UTC  the run. The registers that publish on a daily cycle do so in the
--              small hours (UKGC ~04:30 UTC, Belgium ~02:30 UTC); the README's
--              old 04:17 slot fetched UKGC's previous export.
--   08:00      retries. Each register is skipped when it already has a good
--   10:00      snapshot inside MIN_REFETCH_INTERVAL_H — failed runs do not count
--              — so after a good 06:00 these cost one cheap request each, and
--              after a partial failure only the failed registers run again.
--
-- Supabase-only (pg_cron, pg_net, Vault), so it lives here and not in
-- migrations/, which `registerwatch migrate` applies to any Postgres.
--
-- Before this does anything, create two Vault secrets once, by hand — never in
-- a migration, because migrations are committed:
--
--   select vault.create_secret('https://<your-api-host>',      'registerwatch_api_url');
--   select vault.create_secret('<INGEST_TOKEN from the API env>', 'registerwatch_ingest_token');
--
-- Then check it end to end without waiting for 06:00:
--
--   select registerwatch_private.trigger_ingest();          -- all registers; returns a pg_net request id
--   select registerwatch_private.trigger_ingest('pl_mf');   -- one register
--   select status_code, content from net._http_response order by created desc limit 1;  -- expect 202
--   select * from cron.job_run_details order by start_time desc limit 5;

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Not public: PostgREST exposes public functions as /rpc endpoints, and this
-- one fires a scrape.
create schema if not exists registerwatch_private;
revoke all on schema registerwatch_private from public, anon, authenticated;

drop function if exists registerwatch_private.trigger_ingest(boolean);

create or replace function registerwatch_private.trigger_ingest(slug text default 'all', force boolean default false)
returns bigint
language plpgsql
set search_path = ''
as $$
declare
  api_url text;
  token   text;
begin
  select decrypted_secret into api_url
    from vault.decrypted_secrets where name = 'registerwatch_api_url';
  select decrypted_secret into token
    from vault.decrypted_secrets where name = 'registerwatch_ingest_token';

  -- Raise, don't return: a raised error lands in cron.job_run_details as a
  -- failed run, which is somewhere a human looks. A NULL url would just be a
  -- pg_net error nobody reads.
  if api_url is null or token is null then
    raise exception 'registerwatch: Vault secrets registerwatch_api_url / registerwatch_ingest_token are not set';
  end if;

  return net.http_post(
    url                  := rtrim(api_url, '/') || '/ingest/' || slug || case when force then '?force=true' else '' end,
    headers              := jsonb_build_object(
                              'Authorization', 'Bearer ' || token,
                              'Content-Type',  'application/json'),
    body                 := '{}'::jsonb,
    -- The API answers before it starts fetching; 30s covers a cold start
    -- on a host that sleeps idle services.
    timeout_milliseconds := 30000
  );
end;
$$;

revoke all on function registerwatch_private.trigger_ingest(text, boolean) from public, anon, authenticated;

-- Named, so re-applying updates the job instead of adding a second one.
-- The single-register job this migration used to create, if it ever ran.
select cron.unschedule(jobid) from cron.job where jobname = 'registerwatch-ukgc-daily';

select cron.schedule(
  'registerwatch-daily',
  '0 6,8,10 * * *',
  $$select registerwatch_private.trigger_ingest()$$
);
