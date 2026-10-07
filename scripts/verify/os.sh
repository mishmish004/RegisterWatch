#!/usr/bin/env bash
# Phase 9 (plan.md): the image, run as a container on this host, checked from
# the outside. One PASS/FAIL line per check (T9.3.b has two); exits 1 if any failed.
#
#   docker build -t registerwatch:plan .
#   docker run -d --rm --name rw-pg -e POSTGRES_PASSWORD=rw -e POSTGRES_DB=rw -p 55432:5432 postgres:16-alpine
#   scripts/verify/os.sh                  # every check
#   scripts/verify/os.sh T9.1 T9.3.a      # the checks whose ids start so
#
# Every container uses the host's network, as the plan's `docker run` does, and
# runs read-only with a tmpfs at /tmp. The helpers (os_probe.py, load.py) run
# inside the image under test, so the host needs Docker and python3 (to read
# their JSON). Postgres is migrated, and gb_ukgc gets 5,000 synthetic current
# licences, so T9.4.a's 1000-row page is 1000 rows.
#
#   RW_IMAGE         image under test                 registerwatch:plan
#   RW_IMAGE_BEFORE  also size this one (T9.1.d)      unset
#   RW_DATABASE_URL  Postgres for the app             postgresql://postgres:rw@localhost:55432/rw
#   RW_PG_CONTAINER  that Postgres' container         rw-pg   (paused to make reads slow: T9.2.c, T9.3.c)
#   RW_PORT          port the API listens on          8000
#   RW_LOAD_S        T9.4.a's load, seconds           120     (RSS sampled at half time and at the end)
set -uo pipefail

IMAGE="${RW_IMAGE:-registerwatch:plan}"
DB="${RW_DATABASE_URL:-postgresql://postgres:rw@localhost:55432/rw}"
PG="${RW_PG_CONTAINER:-rw-pg}"
PORT="${RW_PORT:-8000}"
LOAD_S="${RW_LOAD_S:-120}"
BASE="http://127.0.0.1:$PORT"
ROWS="$BASE/v1/registers/gb_ukgc/tables/licences/rows"
UA='RegisterWatch/plan (+https://registerwatch.dev/bot; ops@registerwatch.dev)'
TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
FAILED=0
ONLY=("$@")
CONTAINERS=(rw-api rw-api-ulimit rw-api-init rw-api-limit rw-api-drain rw-api-ingest rw-api-load)

cleanup() {
  docker unpause "$PG" >/dev/null 2>&1 || true
  docker rm -f "${CONTAINERS[@]}" >/dev/null 2>&1 || true
  docker ps -q --filter label=registerwatch.verify=os | xargs -r docker rm -f >/dev/null 2>&1 || true
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
now() { date +%s.%N; }
since() { awk -v a="$1" -v b="$(now)" 'BEGIN { printf "%.2f", b - a }'; }
# py EXPR [FILE]: a Python expression over `d` (FILE's JSON) and `lines` (its JSON lines).
py() { python3 -c "import json, sys
text = open(sys.argv[2]).read() if len(sys.argv) > 2 else ''
lines = [json.loads(l) for l in text.splitlines() if l.strip()]
d = lines[-1] if lines else None
print(eval(sys.argv[1]))" "$@"; }

# api NAME [docker run options...] [-- command...]: the plan's `docker run`, plus options.
api() {
  local name=$1; shift
  local opts=() cmd=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do opts+=("$1"); shift; done
  [ "${1:-}" = "--" ] && { shift; cmd=("$@"); }
  docker rm -f "$name" >/dev/null 2>&1
  docker run -d --name "$name" --network host --read-only --tmpfs /tmp \
    -e DATABASE_URL="$DB" -e INGEST_TOKEN="$TOKEN" -e USER_AGENT="$UA" \
    -e BLOB_BACKEND=local -e SNAPSHOT_ROOT=/tmp/snapshots -e PORT="$PORT" \
    "${opts[@]}" "$IMAGE" "${cmd[@]}" >/dev/null
}
# live NAME [PORT]: wait (up to 30 s) until NAME answers /livez.
live() {
  local i
  for i in $(seq 300); do
    curl -fsS -o /dev/null "http://127.0.0.1:${2:-$PORT}/livez" 2>/dev/null && return 0
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ] || break
    sleep 0.1
  done
  echo "  $1 did not come up:"; docker logs --tail 20 "$1" 2>&1 | sed 's/^/  | /'; return 1
}
gone() { docker rm -f "$@" >/dev/null 2>&1; }
probe() { docker run --rm --label registerwatch.verify=os --network host -e DATABASE_URL="$DB" -e USER_AGENT="$UA" \
            -v "$ROOT/scripts/verify:/verify:ro" --entrypoint python "$IMAGE" /verify/os_probe.py "$@"; }
load() { docker run --rm --label registerwatch.verify=os --network host -v "$ROOT/scripts/verify:/verify:ro" --entrypoint python "$IMAGE" \
           /verify/load.py --base "$BASE" "$@"; }
locks() { probe sql "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory'" > "$TMP/locks" && py 'd[0]["n"]' "$TMP/locks"; }
# run_get ID: the ingest run, as os_probe's get prints it.
run_get() { probe get "$BASE/v1/ingest-runs/$1" --token "$TOKEN" > "$TMP/run"; }
mem_mib() { docker stats --no-stream --format '{{.MemUsage}}' "$1" | python3 -c "
import re, sys
n, unit = re.match(r'([\d.]+)\s*([KMG]i?B)', sys.stdin.read()).groups()
print(round(float(n) * {'KiB': 1/1024, 'MiB': 1, 'GiB': 1024, 'kB': 1/1024, 'MB': 1, 'GB': 1024}[unit], 1))"; }
vm_rss_mib() { docker exec "$1" sh -c "awk '/VmRSS/ {print \$2}' /proc/1/status" | awk '{ printf "%.1f", $1 / 1024 }'; }
fds() { docker exec "$1" sh -c 'ls /proc/1/fd | wc -l'; }

# --- setup -------------------------------------------------------------------------

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "FAIL setup: no image $IMAGE (docker build -t $IMAGE .)"; exit 1; }
if wanted T9.2.c T9.3.b T9.3.c T9.4.a; then
  if ! docker run --rm --network host -e DATABASE_URL="$DB" -e USER_AGENT="$UA" "$IMAGE" registerwatch migrate > "$TMP/migrate" 2>&1; then
    echo "FAIL setup: registerwatch migrate against $DB"; sed 's/^/  | /' "$TMP/migrate" | tail -5; exit 1
  fi
  probe seed --rows 5000 > "$TMP/seed" || { echo "FAIL setup: seeding gb_ukgc"; exit 1; }
  echo "setup: migrated; gb_ukgc licences $(py 'd["current"]' "$TMP/seed") current; image $IMAGE"
fi

# --- P9.1, P9.2 on one container started as the plan says ----------------------------

if wanted T9.1.a T9.1.b T9.1.c T9.2.a T9.2.b; then
  started=$(now)
  api rw-api
  live rw-api || fail T9.1 "rw-api did not start"
fi

if wanted T9.1.a; then
  uid=$(docker exec rw-api id -u 2>&1)
  check T9.1.a "$([ "$uid" = 10001 ] && echo ok)" "docker exec rw-api id -u → $uid ($(docker exec rw-api id -un 2>&1))"
fi

if wanted T9.1.b; then
  ro=$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' rw-api)
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/livez")
  app_write=$(docker exec rw-api sh -c 'touch /app/probe 2>&1' | sed 's/^touch: //')
  tmp_write=$(docker exec rw-api sh -c 'touch /tmp/probe && echo ok' 2>&1)
  check T9.1.b "$([ "$ro" = true ] && [ "$code" = 200 ] && [ -n "$app_write" ] && [ "$tmp_write" = ok ] && echo ok)" \
    "ReadonlyRootfs=$ro, /livez $code; touch /app: ${app_write:-succeeded}; touch /tmp: $tmp_write"
fi

if wanted T9.1.c; then
  health=""
  until [ "$health" = healthy ] || [ "$(since "$started" | cut -d. -f1)" -ge 30 ]; do
    health=$(docker inspect -f '{{.State.Health.Status}}' rw-api); [ "$health" = healthy ] || sleep 0.2
  done
  t=$(since "$started")
  check T9.1.c "$([ "$health" = healthy ] && echo ok)" "Health.Status $health ${t} s after docker run (bound 30 s)"
fi

if wanted T9.1.d; then
  size_of() {  # the unpacked filesystem in bytes, then Docker's .Size
    local c; c=$(docker create "$1"); echo "$(docker export "$c" | wc -c) $(docker image inspect -f '{{.Size}}' "$1")"
    docker rm "$c" >/dev/null
  }
  store=$(docker info -f '{{.Driver}}' 2>/dev/null)  # overlayfs: the containerd store; overlay2: classic
  read -r unpacked inspect <<<"$(size_of "$IMAGE")"
  mb() { awk -v b="$1" 'BEGIN { printf "%.1f MB", b / 1e6 }'; }
  # On the classic store .Size is the unpacked layers; the containerd store adds
  # the compressed layers to it. The bound is on the unpacked filesystem.
  before=""
  if [ -n "${RW_IMAGE_BEFORE:-}" ]; then
    read -r b_unpacked b_inspect <<<"$(size_of "$RW_IMAGE_BEFORE")"
    before="; before ($RW_IMAGE_BEFORE): unpacked $(mb "$b_unpacked"), .Size $(mb "$b_inspect")"
  fi
  check T9.1.d "$([ "$unpacked" -le 250000000 ] && echo ok)" \
    "unpacked (docker export) $(mb "$unpacked"), .Size $(mb "$inspect") [store: ${store:-unknown}] (bound 250 MB)$before"
fi

if wanted T9.1.e; then
  out=$(docker run --rm --entrypoint python "$IMAGE" -c "import registerwatch, importlib.resources as r; print((r.files('registerwatch')/'registers/certs/disig_r2i2.pem').is_file())" 2>&1)
  migrations=$(docker run --rm --entrypoint python "$IMAGE" -c "import importlib.resources as r; print(sum(p.name.endswith('.sql') for p in (r.files('registerwatch')/'migrations').iterdir()))" 2>&1)
  check T9.1.e "$([ "$out" = True ] && [ "$migrations" -ge 1 ] 2>/dev/null && echo ok)" \
    "disig_r2i2.pem is_file → $out; $migrations migrations in the package"
fi

if wanted T9.2.a; then
  cmdline=$(docker exec rw-api cat /proc/1/cmdline | tr '\0' ' ' | sed 's/ $//')
  check T9.2.a "$([[ "$cmdline" =~ python[0-9.]*\ /app/.venv/bin/registerwatch\ serve$ ]] && echo ok)" "PID 1: $cmdline"
fi

if wanted T9.2.b; then
  lim() { docker exec "$1" sh -c 'cat /proc/1/limits' | awk '/Max open files/ { print $4, $5 }'; }
  read -r soft hard <<<"$(lim rw-api)"
  # A platform that starts containers at a soft 1024 (the hard limit as here):
  # the server raises its own.
  api rw-api-ulimit --ulimit "nofile=1024:$hard" -e PORT=$((PORT + 1))
  live rw-api-ulimit $((PORT + 1))
  read -r soft2 hard2 <<<"$(lim rw-api-ulimit)"
  gone rw-api-ulimit
  check T9.2.b "$([ "$soft" -ge 4096 ] && [ "$soft2" -ge 4096 ] 2>/dev/null && echo ok)" \
    "Max open files $soft (hard $hard) as started here; started with --ulimit nofile=1024:$hard, the server raised it to $soft2 (hard $hard2)"
fi

gone rw-api

if wanted T9.2.c; then
  # 20 reads at once while the database is paused, so admitted ones wait on it.
  api rw-api-limit -e DB_POOL_TIMEOUT_S=30 -- registerwatch serve --limit-concurrency 5
  live rw-api-limit && probe get "$ROWS?limit=10" > /dev/null
  docker pause "$PG" >/dev/null
  probe burst "$ROWS?limit=10" --n 20 > "$TMP/burst" &
  bp=$!; sleep 2; docker unpause "$PG" >/dev/null; wait $bp
  verdict=$(py '"ok" if d["errors"] == 0 and set(d["statuses"]) <= {"200", "503"} and 1 <= d["statuses"].get("200", 0) <= 5 and all(l["ms"] < 100 for l in lines[:-1] if l["status"] == 503) and d["max_ms"] < 10000 else "no"' "$TMP/burst")
  check T9.2.c "$verdict" "$(py '"%s; 503 in %s–%s ms (%s), the admitted 200s in %s–%s ms, the database paused for 2 s" % (d["statuses"], min([l["ms"] for l in lines[:-1] if l["status"] == 503] or [0]), max([l["ms"] for l in lines[:-1] if l["status"] == 503] or [0]), next((l["type"] for l in lines[:-1] if l["status"] == 503), "-"), min([l["ms"] for l in lines[:-1] if l["status"] == 200] or [0]), max([l["ms"] for l in lines[:-1] if l["status"] == 200] or [0]))' "$TMP/burst")"
  gone rw-api-limit
fi

# --- P9.3 ----------------------------------------------------------------------------

if wanted T9.3.a; then
  stop_idle() {  # NAME [options]: start, wait until healthy, then time `docker stop -t 30`
    local name=$1; shift
    api "$name" "$@"; live "$name"
    local i; for i in $(seq 150); do [ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ] && break; sleep 0.2; done
    local s; s=$(now); docker stop -t 30 "$name" >/dev/null; echo "$(since "$s") $(docker inspect -f '{{.State.ExitCode}}' "$name")"
  }
  read -r t1 code1 <<<"$(stop_idle rw-api)"
  # Under an init (tini as PID 1) the server is PID 2, where an unhandled SIGTERM would kill it (143).
  read -r t2 code2 <<<"$(stop_idle rw-api-init --init)"
  gone rw-api rw-api-init
  check T9.3.a "$(awk -v a="$t1" -v b="$t2" 'BEGIN { exit !(a < 3 && b < 3) }' && [ "$code1" = 0 ] && [ "$code2" = 0 ] && echo ok)" \
    "docker stop -t 30, idle: ${t1} s, exit $code1; as PID 2 under --init: ${t2} s, exit $code2 (bound 3 s, exit 0)"
fi

if wanted T9.3.b; then
  # start_run NAME: a 4-register run on the fake slow engine (20 s each), in hand for 2 s.
  start_run() {
    probe get "$BASE/v1/ingest-runs" --method POST --token "$TOKEN" \
      --body '{"registers": ["gb_ukgc", "de_ggl", "fr_anj", "pl_mf"]}' > "$TMP/created"
    run_id=$(py 'd["body"]["id"]' "$TMP/created")
    local i; for i in $(seq 50); do run_get "$run_id"; [ "$(py 'd["body"]["status"]' "$TMP/run")" = running ] && break; sleep 0.1; done
    sleep 2
  }
  grace=60
  api rw-api-ingest -e REGISTERWATCH_FAKE_SLOW_INGEST=1; live rw-api-ingest
  start_run; status_before=$(py 'd["body"]["status"]' "$TMP/run")
  s=$(now); docker stop -t 90 rw-api-ingest >/dev/null; t=$(since "$s"); code=$(docker inspect -f '{{.State.ExitCode}}' rw-api-ingest)
  locks_down=$(locks)
  docker start rw-api-ingest >/dev/null; live rw-api-ingest; run_get "$run_id"; locks_up=$(locks)
  summary=$(py '"%s, not_started %s, %d result(s) (%s)" % (d["body"]["status"], d["body"]["not_started"], len(d["body"]["results"]), ", ".join(r["register"] + " " + str(r["reason"]) for r in d["body"]["results"]))' "$TMP/run")
  verdict=$(py '"ok" if d["body"]["status"] == "partial" and d["body"]["not_started"] == ["de_ggl", "fr_anj", "pl_mf"] and len(d["body"]["results"]) == 1 else "no"' "$TMP/run")
  logged=$(docker logs rw-api-ingest 2>&1 | grep -c 'register(s) not started')
  gone rw-api-ingest
  check T9.3.b "$([ "$verdict" = ok ] && [ "$status_before" = running ] && awk -v t="$t" -v g=$grace 'BEGIN { exit !(t <= g + 10) }' && [ "$code" = 0 ] && [ "$locks_down" = 0 ] && [ "$locks_up" = 0 ] && echo ok)" \
    "run $status_before at the stop; docker stop -t 90 took ${t} s (bound SHUTDOWN_GRACE_S + 10 = $((grace + 10)) s), exit $code; after restart: $summary; advisory locks: $locks_down stopped, $locks_up restarted"

  # The bound: a register that outlives SHUTDOWN_GRACE_S is left behind, and the sweep fails its run.
  grace=5
  api rw-api-ingest -e REGISTERWATCH_FAKE_SLOW_INGEST=1 -e SHUTDOWN_GRACE_S=$grace; live rw-api-ingest
  start_run; status_before=$(py 'd["body"]["status"]' "$TMP/run")
  s=$(now); docker stop -t 90 rw-api-ingest >/dev/null; t=$(since "$s"); code=$(docker inspect -f '{{.State.ExitCode}}' rw-api-ingest)
  left=$(docker logs rw-api-ingest 2>&1 | grep -c 'exiting without it')
  locks_down=$(locks)
  docker start rw-api-ingest >/dev/null; live rw-api-ingest; run_get "$run_id"; locks_up=$(locks)
  summary=$(py '"%s, error %r, not_started %s, %d result(s)" % (d["body"]["status"], d["body"]["error"], d["body"]["not_started"], len(d["body"]["results"]))' "$TMP/run")
  verdict=$(py '"ok" if d["body"]["status"] == "failed" and d["body"]["error"] == "worker lost" else "no"' "$TMP/run")
  gone rw-api-ingest
  check "T9.3.b bound" "$([ "$verdict" = ok ] && [ "$status_before" = running ] && awk -v t="$t" -v g=$grace 'BEGIN { exit !(t <= g + 10) }' && [ "$code" = 1 ] && [ "$left" -ge 1 ] && [ "$locks_down" = 0 ] && [ "$locks_up" = 0 ] && echo ok)" \
    "SHUTDOWN_GRACE_S=$grace, register in hand for 20 s: docker stop took ${t} s (bound $((grace + 10)) s), exit $code, 'exiting without it' logged $left×; after restart: $summary; advisory locks: $locks_down stopped, $locks_up restarted"
fi

if wanted T9.3.c; then
  # 10 reads in flight on a paused database when the stop comes; it lets them finish.
  api rw-api-drain -e DB_POOL_TIMEOUT_S=30; live rw-api-drain && probe get "$ROWS?limit=100" > /dev/null
  docker pause "$PG" >/dev/null
  probe burst "$ROWS?limit=100" --n 10 > "$TMP/drain" &
  bp=$!; sleep 1
  s=$(now); (docker stop -t 30 rw-api-drain >/dev/null; since "$s" > "$TMP/stopped") &
  sp=$!; sleep 2
  draining=$(docker inspect -f '{{.State.Running}}' rw-api-drain)
  refused=$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "$BASE/livez")
  docker unpause "$PG" >/dev/null; wait $bp $sp
  code=$(docker inspect -f '{{.State.ExitCode}}' rw-api-drain)
  verdict=$(py '"ok" if d["statuses"] == {"200": 10} and d["errors"] == 0 else "no"' "$TMP/drain")
  check T9.3.c "$([ "$verdict" = ok ] && [ "$draining" = true ] && [ "$code" = 0 ] && echo ok)" \
    "$(py 'str(d["statuses"]) + ", slowest %s ms" % d["max_ms"]' "$TMP/drain"); 2 s into the stop: still running ($draining), a new /livez → ${refused/000/refused}; database resumed, then the stop finished at $(cat "$TMP/stopped") s, exit $code"
  gone rw-api-drain
fi

# --- P9.4 ----------------------------------------------------------------------------

if wanted T9.4.a; then
  # 50 rps from one address is 3,000 reads a minute: the read limit is raised
  # above it (not off), so every request still goes through the limiter.
  api rw-api-load -e RATE_LIMIT_READ_PER_MIN=6000; live rw-api-load
  probe get "$ROWS?limit=1000" > "$TMP/one"
  rows=$(py 'len(d["body"]["data"])' "$TMP/one")
  sleep 10
  fd_idle=$(fds rw-api-load); mem_idle=$(mem_mib rw-api-load); rss_idle=$(vm_rss_mib rw-api-load)
  load --rps 50 --duration "$LOAD_S" --path "/v1/registers/gb_ukgc/tables/licences/rows?limit=1000" > "$TMP/load" &
  lp=$!; s=$(now)
  # "Minute 1" and "minute 2": half way and at the end of the schedule, by the clock.
  sleep "$(awk -v d="$LOAD_S" 'BEGIN { print d / 2 }')"
  mem1=$(mem_mib rw-api-load); rss1=$(vm_rss_mib rw-api-load); fd_load=$(fds rw-api-load)
  sleep "$(awk -v d="$LOAD_S" -v t="$(since "$s")" 'BEGIN { r = d - t; print (r > 0 ? r : 0) }')"
  mem2=$(mem_mib rw-api-load); rss2=$(vm_rss_mib rw-api-load)
  wait $lp; load_code=$?
  sleep 10
  fd_after=$(fds rw-api-load)
  conns=$(probe sql "SELECT application_name AS a, count(*) AS n FROM pg_stat_activity WHERE application_name LIKE 'registerwatch%' GROUP BY 1 ORDER BY 1" | tr -d '\n')
  growth=$(awk -v a="$mem1" -v b="$mem2" 'BEGIN { printf "%.1f", (b - a) / a * 100 }')
  rss_growth=$(awk -v a="$rss1" -v b="$rss2" 'BEGIN { printf "%.1f", (b - a) / a * 100 }')
  fd_delta=$((fd_after - fd_idle))
  check T9.4.a "$([ "$rows" = 1000 ] && [ $load_code = 0 ] && awk -v g="$growth" 'BEGIN { exit !(g < 20) }' && [ ${fd_delta#-} -le 10 ] && echo ok)" \
    "$(cat "$TMP/load"); docker stats memory idle $mem_idle, at $((LOAD_S / 2)) s $mem1, at $LOAD_S s $mem2 MiB (growth $growth %, bound 20 %); VmRSS $rss_idle, $rss1, $rss2 MiB ($rss_growth %); fds idle $fd_idle, under load $fd_load, 10 s after $fd_after (Δ $fd_delta, bound 10); pg connections after: $conns; $rows rows a page"
  gone rw-api-load
fi

exit $FAILED
