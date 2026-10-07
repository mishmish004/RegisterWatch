#!/usr/bin/env bash
# Phase 11 (plan.md): the Supabase schedule starts v1 ingest runs. One PASS/FAIL
# line per check; exits 1 if any failed.
#
#   docker build -t registerwatch:plan .
#   scripts/verify/cron.sh                        # every check, on a local stand-in
#   scripts/verify/cron.sh T11.1.b                # the checks whose ids start so
#   RW_SUPABASE_DB_URL='postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres' \
#   RW_EDGE=https://<api-host> RW_INGEST_TOKEN=<the API's INGEST_TOKEN> scripts/verify/cron.sh
#
# With RW_SUPABASE_DB_URL, the checks run against that project and the API at
# RW_EDGE, whose DATABASE_URL is the same database: T11.1.b starts a real ingest
# of pl_mf, and T11.1.c reads what the last slot of `registerwatch-daily` did.
# The host needs psql, curl and python3, nothing else.
#
# Without it, a local stand-in: Supabase's own Postgres image (pg_cron, pg_net
# and Vault preloaded; `postgres` is not a superuser, as on a hosted project)
# with every supabase/migrations file applied in order as `postgres`, as
# `supabase db push` does, and the image under test using that database, as the
# deployment does. Vault gets the stand-in API's address and a generated
# token. T11.1.c makes the daily job fire twice, a minute apart, then puts its
# schedule back. Every register's fetch fails there (no route to the
# regulators), so the stand-in cannot show freshness; that is the deployment's.
#
#   RW_SUPABASE_DB_URL  the project's database (psql)          unset: the local stand-in
#   RW_EDGE             the API Vault's URL points at          (local: http://127.0.0.1:$RW_PORT)
#   RW_INGEST_TOKEN     the API's INGEST_TOKEN                 (local: generated)
#   RW_IMAGE            image under test (local)               registerwatch:plan
#   RW_SUPABASE_IMAGE   Supabase's Postgres (local)            mirror.gcr.io/supabase/postgres:17.11.0.004
#   RW_PORT             host ports: the API, and the next for Postgres   8100
set -uo pipefail

IMAGE="${RW_IMAGE:-registerwatch:plan}"
SB_IMAGE="${RW_SUPABASE_IMAGE:-mirror.gcr.io/supabase/postgres:17.11.0.004}"
PORT="${RW_PORT:-8100}"
UA='RegisterWatch/plan (+https://registerwatch.dev/bot; ops@registerwatch.dev)'
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
FAILED=0
ONLY=("$@")
NET=rw-cron-net
CONTAINERS=(rw-cron-sb rw-cron-app)
DAILY='0 6,8,10 * * *'
JOB=''

cleanup() {
  # A stand-in only; a real project's job is never left changed by this script.
  [ -n "$JOB" ] && [ -z "${RW_SUPABASE_DB_URL:-}" ] && sql "select cron.alter_job($JOB, schedule := '$DAILY')" >/dev/null 2>&1
  if [ -z "${RW_SUPABASE_DB_URL:-}" ]; then
    docker rm -f "${CONTAINERS[@]}" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

# --- helpers -----------------------------------------------------------------------

pass() { echo "PASS $1: $2"; }
fail() { echo "FAIL $1: $2"; FAILED=1; }
check() { if [ "$2" = ok ]; then pass "$1" "$3"; else fail "$1" "$3"; fi; }
wanted() {  # wanted ID...: any of these was asked for
  [ ${#ONLY[@]} -eq 0 ] && return 0
  local id p; for id in "$@"; do for p in "${ONLY[@]}"; do [[ "$id" == "$p"* ]] && return 0; done; done
  return 1
}
sql() { psql "$SBDB" -v ON_ERROR_STOP=1 -XtAq -F '|' -c "$1"; }
# py EXPR [ARG...]: a Python expression over `a` (the arguments), json and iso (an RFC 3339 time).
py() { python3 -c "import json, sys
from datetime import datetime
iso = lambda t: datetime.fromisoformat(t.replace('Z', '+00:00'))
a = sys.argv[2:]
print(eval(sys.argv[1]))" "$@"; }
# response ID: pg_net's answer to request ID (`status|error|body`), waiting up to 40 s
# (the function's timeout is 30 s).
response() {
  local i out
  for i in $(seq 80); do
    out=$(sql "select coalesce(status_code::text, ''), coalesce(error_msg, ''), coalesce(content, '') from net._http_response where id = $1")
    [ -n "$out" ] && { echo "$out"; return 0; }
    sleep 0.5
  done
  echo "||"
}
run_of() { py 'json.loads(a[0]).get("id", "") if a[0].startswith("{") else ""' "$1"; }
# clear_of_the_hour SECONDS: wait for the next UTC hour if this one has less than that
# left, so the calls a check compares share an Idempotency-Key.
clear_of_the_hour() {
  local left; left=$(sql "select 3600 - extract(epoch from clock_timestamp() - date_trunc('hour', clock_timestamp()))::int")
  if [ "$left" -lt "$1" ]; then echo "  waiting ${left}s for the next UTC hour"; sleep $((left + 2)); fi
}
api() { curl -sS --max-time 30 -H "Authorization: Bearer $INGEST_TOKEN" "$@"; }

# --- setup -------------------------------------------------------------------------

if [ -n "${RW_SUPABASE_DB_URL:-}" ]; then
  : "${RW_EDGE:?RW_EDGE: the API's https base URL}" "${RW_INGEST_TOKEN:?RW_INGEST_TOKEN: the API's INGEST_TOKEN}"
  SBDB="$RW_SUPABASE_DB_URL"; API="${RW_EDGE%/}"; INGEST_TOKEN="$RW_INGEST_TOKEN"
  WHERE="$(py 'a[0].split("@")[-1].split("/")[0]' "$SBDB") and $API"
  vault=$(sql "select decrypted_secret from vault.decrypted_secrets where name = 'registerwatch_api_url'") \
    || { echo "FAIL setup: cannot read Vault on $WHERE"; exit 1; }
  [ "${vault%/}" = "$API" ] || echo "  note: Vault's registerwatch_api_url is $vault, not $API"
else
  docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "FAIL setup: no image $IMAGE (docker build -t $IMAGE .)"; exit 1; }
  SB_PORT=$((PORT + 1)); SBDB="postgresql://postgres:rw@127.0.0.1:$SB_PORT/postgres"; API="http://127.0.0.1:$PORT"
  INGEST_TOKEN=$(python3 -c 'import secrets; print("t" + secrets.token_urlsafe(32))')
  docker rm -f "${CONTAINERS[@]}" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1
  docker network create "$NET" >/dev/null
  docker run -d --name rw-cron-sb --network "$NET" -e POSTGRES_PASSWORD=rw -p "127.0.0.1:$SB_PORT:5432" "$SB_IMAGE" >/dev/null \
    || { echo "FAIL setup: cannot start $SB_IMAGE"; exit 1; }
  for i in $(seq 120); do sql 'select 1' >/dev/null 2>&1 && break; sleep 0.5; done
  # The image's entrypoint restarts Postgres once after initialising it: wait for the second start too.
  sleep 3; for i in $(seq 120); do sql 'select 1' >/dev/null 2>&1 && break; sleep 0.5; done
  for f in "$ROOT"/supabase/migrations/*.sql; do
    psql "$SBDB" -v ON_ERROR_STOP=1 -Xq -f "$f" > "$TMP/migrate" 2>&1 \
      || { echo "FAIL setup: $(basename "$f") on $SB_IMAGE"; sed 's/^/  | /' "$TMP/migrate" | tail -5; exit 1; }
  done
  docker run -d --name rw-cron-app --network "$NET" --read-only --tmpfs /tmp -p "127.0.0.1:$PORT:8000" \
    -e DATABASE_URL=postgresql://postgres:rw@rw-cron-sb:5432/postgres -e INGEST_TOKEN="$INGEST_TOKEN" \
    -e USER_AGENT="$UA" -e BLOB_BACKEND=local -e SNAPSHOT_ROOT=/tmp/snapshots "$IMAGE" >/dev/null
  for i in $(seq 120); do curl -fsS --noproxy '*' -o /dev/null "$API/livez" 2>/dev/null && break; sleep 0.25; done
  export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1" no_proxy="${no_proxy:+$no_proxy,}127.0.0.1"
  sql "select vault.create_secret('http://rw-cron-app:8000', 'registerwatch_api_url')" >/dev/null
  sql "select vault.create_secret('$INGEST_TOKEN', 'registerwatch_ingest_token')" >/dev/null
  version=$(sql 'show server_version'); cron=$(sql "select extversion from pg_extension where extname = 'pg_cron'")
  net=$(sql "select extversion from pg_extension where extname = 'pg_net'")
  WHERE="local stand-in ($SB_IMAGE: Postgres $version, pg_cron $cron, pg_net $net)"
  echo "setup: $(ls "$ROOT"/supabase/migrations/*.sql | wc -l) migrations applied as postgres; $WHERE; image $IMAGE"
fi

# --- T11.1.a's subject, as the database has it ---------------------------------------

def=$(sql "select pg_get_functiondef('registerwatch_private.trigger_ingest(text, boolean)'::regprocedure)")
case "$def" in
  *"/v1/ingest-runs"*"Idempotency-Key"*) ;;
  *) echo "FAIL setup: trigger_ingest on $WHERE is not the v1 one (apply supabase/migrations/20261007000010_schedule_v1.sql)"; exit 1 ;;
esac

# --- T11.1.b a manual trigger, twice in one hour, makes one run ----------------------

if wanted T11.1.b; then
  # One run at a time: while another goes, the first call would be a 409.
  for i in $(seq 600); do
    [ "$(sql "select count(*) from ingest_runs where status in ('queued', 'running')")" = 0 ] && break
    [ "$i" = 1 ] && echo "  a run is going: waiting up to 10 minutes for it to finish"; sleep 1
  done
  clear_of_the_hour 60
  t0=$(sql 'select clock_timestamp()')
  first=$(sql "select registerwatch_private.trigger_ingest('pl_mf')")
  IFS='|' read -r s1 e1 b1 <<< "$(response "$first")"
  second=$(sql "select registerwatch_private.trigger_ingest('pl_mf')")
  IFS='|' read -r s2 e2 b2 <<< "$(response "$second")"
  r1=$(run_of "$b1"); r2=$(run_of "$b2")
  key=$(sql "select idempotency_key from ingest_runs where id = '${r1:-00000000-0000-0000-0000-000000000000}'")
  api -o "$TMP/runs" "$API/v1/ingest-runs?limit=100"
  listed=$(py 'len([r for r in json.load(open(a[0]))["data"] if iso(r["created_at"]) >= iso(a[1])])' "$TMP/runs" \
    "$(sql "select to_char('$t0'::timestamptz at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")" 2>/dev/null)
  has=$(py 'sum(r["id"] == a[1] for r in json.load(open(a[0]))["data"])' "$TMP/runs" "$r1" 2>/dev/null)
  check T11.1.b "$([ "$s1" = 202 ] && [ "$s2" = 200 ] && [ -n "$r1" ] && [ "$r1" = "$r2" ] && [ "$listed" = 1 ] && [ "$has" = 1 ] && echo ok)" \
    "$WHERE: trigger_ingest('pl_mf') → request $first: ${s1:-no answer}${e1:+ ($e1)} run ${r1:-none}; again → request $second: ${s2:-no answer}${e2:+ ($e2)} run ${r2:-none}; key ${key:-none}; GET /v1/ingest-runs: ${listed:-?} run(s) created since the first call, that one listed ${has:-?}×"
fi

# --- T11.1.c the daily slot ------------------------------------------------------------

if wanted T11.1.c; then
  JOB=$(sql "select jobid from cron.job where jobname = 'registerwatch-daily'")
  job=$(sql "select schedule || ' | ' || command from cron.job where jobid = ${JOB:-0}")
  if [ -n "${RW_SUPABASE_DB_URL:-}" ]; then
    JOB_SEEN=$JOB; JOB=''  # read only: cleanup never touches a real project's job
    IFS='|' read -r st msg started hour <<< "$(sql "select status, return_message, start_time, to_char(start_time at time zone 'UTC', 'YYYY-MM-DD-HH24') from cron.job_run_details where jobid = ${JOB_SEEN:-0} order by start_time desc limit 1")"
    IFS='|' read -r run rstatus <<< "$(sql "select id, status from ingest_runs where idempotency_key = 'registerwatch-cron-$hour-all'")"
    api -o "$TMP/status" "$API/v1/status"
    stale=$(py 'json.load(open(a[0]))["stale_registers"]' "$TMP/status" 2>/dev/null)
    # `succeeded` is only the SQL: pg_net's answer is not in it (the legacy function's 409s were `succeeded` too).
    check T11.1.c "$([ "$st" = succeeded ] && [ -n "$run" ] && [ "$stale" = "[]" ] && echo ok)" \
      "$WHERE: job $job; its last run at $started: ${st:-none} ($msg); the slot's v1 run (key registerwatch-cron-$hour-all): ${run:-none} ${rstatus}; /v1/status stale_registers ${stale:-unreadable}"
  else
    clear_of_the_hour 200
    t0=$(sql 'select clock_timestamp()')
    sql "select cron.alter_job($JOB, schedule := '* * * * *')" >/dev/null
    for i in $(seq 300); do
      [ "$(sql "select count(*) from cron.job_run_details where jobid = $JOB and start_time >= '$t0' and status in ('succeeded', 'failed')")" -ge 2 ] && break
      sleep 0.5
    done
    sql "select cron.alter_job($JOB, schedule := '$DAILY')" >/dev/null; JOB_SEEN=$JOB; JOB=''
    runs=$(sql "select string_agg(status || ' at ' || to_char(start_time, 'HH24:MI:SS'), ', ' order by start_time) from cron.job_run_details where jobid = $JOB_SEEN and start_time >= '$t0'")
    ok_runs=$(sql "select count(*) from cron.job_run_details where jobid = $JOB_SEEN and start_time >= '$t0' and status = 'succeeded'")
    sleep 2; ids=$(sql "select string_agg(id::text, ' ' order by id) from net._http_response where created >= '$t0'")
    answers=""; for id in $ids; do IFS='|' read -r s e b <<< "$(response "$id")"; answers+="${answers:+, }$s $(run_of "$b")"; done
    IFS='|' read -r n key <<< "$(sql "select count(*), max(idempotency_key) from ingest_runs where created_at >= '$t0'")"
    after=$(sql "select schedule from cron.job where jobid = $JOB_SEEN")
    statuses=$(py '",".join(x.split()[0] for x in a[0].split(", ") if x)' "$answers")
    check T11.1.c "$([ "$ok_runs" = 2 ] && [ "$statuses" = 202,200 ] && [ "$n" = 1 ] && [[ "$key" == registerwatch-cron-*-all ]] && [ "$after" = "$DAILY" ] && echo ok)" \
      "$WHERE: job registerwatch-daily ($job) set to every minute: $runs; pg_net answers $answers; $n run(s) created, key $key; schedule put back to '$after'. Freshness not checked here (no route to the regulators)"
  fi
fi

exit $FAILED
