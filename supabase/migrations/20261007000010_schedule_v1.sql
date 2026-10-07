-- 0010 — the daily schedule starts v1 ingest runs (plan.md Phase 11).
--
-- 0004's job and slots stay as they are (06:00, 08:00, 10:00 UTC, job
-- `registerwatch-daily`, which calls trigger_ingest()). Only the function
-- changes: it POSTs /v1/ingest-runs instead of the legacy /ingest/{slug}, with
-- the same signature, the same Vault secrets and the same token. Nothing needs
-- setting up again.
--
--   trigger_ingest()               body {}                                  every register
--   trigger_ingest('pl_mf')        body {"registers": ["pl_mf"]}
--   trigger_ingest('pl_mf', true)  body {"registers": ["pl_mf"], "force": true}
--
-- Each call names its slot in an Idempotency-Key, the UTC date and hour:
--
--   registerwatch-cron-2026-10-08-06-all
--   registerwatch-cron-2026-10-08-06-pl_mf-force
--
-- so a slot that fires twice, or a manual call repeated in the same UTC hour,
-- starts one run: the API answers the repeat with the original run (200)
-- instead of starting another or answering 409. The 08:00 and 10:00 slots
-- have keys of their own, so they still retry what failed at 06:00. A forced
-- call's key ends in `-force`, because the API refuses a key reused with a
-- different body (422): a forced re-run straight after an unforced call is a
-- run of its own.
--
-- Every run is now a row the API keeps: GET /v1/ingest-runs (ingest token)
-- lists them, whichever replica ran them.
--
-- Check it without waiting for 06:00 (plan.md T11.1.b):
--
--   select registerwatch_private.trigger_ingest('pl_mf');                         -- a pg_net request id
--   select status_code, content from net._http_response order by created desc limit 1;  -- 202, the run
--   select registerwatch_private.trigger_ingest('pl_mf');                         -- again, the same hour
--   select status_code, content from net._http_response order by created desc limit 1;  -- 200, the same run
--
-- To go back to the legacy route, re-run 0004's `create or replace function`.
-- The legacy routes stay in the API until plan.md Phase 12.

create or replace function registerwatch_private.trigger_ingest(slug text default 'all', force boolean default false)
returns bigint
language plpgsql
set search_path = ''
as $$
declare
  api_url text;
  token   text;
  body    jsonb := '{}'::jsonb;
begin
  select decrypted_secret into api_url
    from vault.decrypted_secrets where name = 'registerwatch_api_url';
  select decrypted_secret into token
    from vault.decrypted_secrets where name = 'registerwatch_ingest_token';

  -- Raise, don't return: a raised error lands in cron.job_run_details as a
  -- failed run, which is somewhere a human looks.
  if api_url is null or token is null then
    raise exception 'registerwatch: Vault secrets registerwatch_api_url / registerwatch_ingest_token are not set';
  end if;

  if slug <> 'all' then
    body := jsonb_build_object('registers', jsonb_build_array(slug));
  end if;
  if force then
    body := body || jsonb_build_object('force', true);
  end if;

  return net.http_post(
    url                  := rtrim(api_url, '/') || '/v1/ingest-runs',
    headers              := jsonb_build_object(
                              'Authorization',   'Bearer ' || token,
                              'Content-Type',    'application/json',
                              'Idempotency-Key', 'registerwatch-cron-'
                                                 || to_char(now() at time zone 'UTC', 'YYYY-MM-DD-HH24')
                                                 || '-' || slug
                                                 || case when force then '-force' else '' end),
    body                 := body,
    -- The API answers before it starts fetching; 30s covers a cold start
    -- on a host that sleeps idle services.
    timeout_milliseconds := 30000
  );
end;
$$;

revoke all on function registerwatch_private.trigger_ingest(text, boolean) from public, anon, authenticated;
