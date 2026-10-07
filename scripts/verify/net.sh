#!/usr/bin/env bash
# Phase 10 (plan.md): the network level, checked from the outside. One PASS/FAIL
# line per check; exits 1 if any failed.
#
#   docker build -t registerwatch:plan .
#   docker run -d --rm --name rw-pg -e POSTGRES_PASSWORD=rw -e POSTGRES_DB=rw -p 55432:5432 postgres:16-alpine
#   scripts/verify/net.sh                       # every check
#   scripts/verify/net.sh T10.3 T10.6.a         # the checks whose ids start so
#   RW_EDGE=https://staging.example scripts/verify/net.sh T10.1.a T10.1.b T10.1.c T10.3.d T10.6.c
#
# [box] checks run the image as containers on this host's network, read-only
# with a tmpfs at /tmp, as os.sh does. [edge] checks run against RW_EDGE, a
# deployment behind its platform's TLS proxy; without one, against a local
# stand-in: Caddy (scripts/verify/edge/Caddyfile) terminating TLS with its own
# CA in front of the image, on a Docker network of their own, the app trusting
# only Caddy's address for X-Forwarded-*. The helpers (net_probe.py, os_probe.py,
# load.py) run inside the image under test. The host needs Docker, python3, curl
# and openssl, and for T10.1.d node with Playwright's Chromium. Postgres is
# migrated and loaded with every register's fixture rows (tests/fixtures), and
# gb_ukgc gets 5,000 synthetic current licences, so a 1000-row page is 1000 rows.
#
#   RW_IMAGE          image under test                         registerwatch:plan
#   RW_DATABASE_URL   Postgres, as the host reaches it          postgresql://postgres:rw@localhost:55432/rw
#   RW_PORT           first port the [box] containers use       8000 (and the next five)
#   RW_EDGE           staging base URL for [edge] checks       unset: the local Caddy edge
#   RW_EDGE_HOST      the local edge's name                    rw.test
#   RW_WORKERS        WEB_CONCURRENCY for T10.6.a               2
#   RW_LOAD_S         T10.6.a's duration, seconds               120
#   RW_DOCS_MIRROR    jsDelivr's npm packages on disk (T10.1.d), for a host that cannot reach the CDN
set -uo pipefail

IMAGE="${RW_IMAGE:-registerwatch:plan}"
DB="${RW_DATABASE_URL:-postgresql://postgres:rw@localhost:55432/rw}"
PORT="${RW_PORT:-8000}"
WORKERS="${RW_WORKERS:-2}"
LOAD_S="${RW_LOAD_S:-120}"
EDGE_HOST="${RW_EDGE_HOST:-rw.test}"
UA='RegisterWatch/plan (+https://registerwatch.dev/bot; ops@registerwatch.dev)'
TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
READ=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
FAILED=0
ONLY=("$@")
ROWS=/v1/registers/gb_ukgc/tables/licences/rows
# T10.6's mix: 70 % rows, 20 % domains, 10 % search, over a few real values each.
MIX=(--mix "rows=70:$ROWS" --mix 'domains=10:/v1/domains/bet365.com' --mix 'domains=10:/v1/domains/pokerstars.com'
     --mix 'search=4:/v1/search?q=betway' --mix 'search=3:/v1/search?q=casino' --mix 'search=3:/v1/search?q=stars')
NET=rw-edge-net SUBNET=172.31.250.0/24 EDGE_IP=172.31.250.2 APP_IP=172.31.250.10
CONTAINERS=(rw-net rw-net-cors rw-net-xff rw-net-xff-untrusted rw-net-load rw-net-limit rw-edge rw-edge-app)

cleanup() {
  docker rm -f "${CONTAINERS[@]}" >/dev/null 2>&1 || true
  docker ps -q --filter label=registerwatch.verify=net | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
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
# py EXPR [FILE]: a Python expression over `d` (FILE's last JSON line) and `lines` (all of them).
py() { python3 -c "import json, sys
text = open(sys.argv[2]).read() if len(sys.argv) > 2 else ''
lines = [json.loads(l) for l in text.splitlines() if l.strip().startswith('{')]
d = lines[-1] if lines else None
print(eval(sys.argv[1]))" "$@"; }
# header NAME FILE: a header's value from curl -D output (the last response's).
header() { tr -d '\r' < "$2" | awk -v n="$(echo "$1" | tr 'A-Z' 'a-z')" 'BEGIN { IGNORECASE = 1 } /^HTTP\// { v = "" } tolower($0) ~ "^" n ":" { sub(/^[^:]*:[ ]*/, ""); v = $0 } END { print v }'; }
status() { tr -d '\r' < "$1" | awk '/^HTTP\// { s = $2 } END { print s }'; }

# api NAME PORT [docker run options...]: the image on this host's network, as os.sh runs it.
api() {
  local name=$1 port=$2; shift 2
  docker rm -f "$name" >/dev/null 2>&1
  docker run -d --name "$name" --network host --read-only --tmpfs /tmp \
    -e DATABASE_URL="$DB" -e INGEST_TOKEN="$TOKEN" -e USER_AGENT="$UA" \
    -e BLOB_BACKEND=local -e SNAPSHOT_ROOT=/tmp/snapshots -e PORT="$port" "$@" "$IMAGE" >/dev/null
  live "$name" "http://127.0.0.1:$port"
}
# live NAME BASE: wait (up to 30 s) until BASE answers /livez.
live() {
  local i
  for i in $(seq 300); do
    curl -fsS -o /dev/null --noproxy '*' "$2/livez" 2>/dev/null && return 0
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ] || break
    sleep 0.1
  done
  echo "  $1 did not come up:"; docker logs --tail 20 "$1" 2>&1 | sed 's/^/  | /'; return 1
}
gone() { docker rm -f "$@" >/dev/null 2>&1; }
helper() {  # helper [docker run options...] -- SCRIPT ARGS...: a helper script inside the image
  local opts=(); while [ "$1" != "--" ]; do opts+=("$1"); shift; done; shift
  docker run --rm --label registerwatch.verify=net --no-healthcheck --network host -e DATABASE_URL="$DB" -e USER_AGENT="$UA" \
    -v "$ROOT/scripts/verify:/verify:ro" "${opts[@]}" --entrypoint python "$IMAGE" "/verify/$1" "${@:2}"
}
c() { curl -sS --noproxy '*' "$@"; }  # [box] requests go straight to the container

# --- setup -------------------------------------------------------------------------

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "FAIL setup: no image $IMAGE (docker build -t $IMAGE .)"; exit 1; }
if wanted T10.1.d T10.4 T10.6; then
  if ! docker run --rm --network host -e DATABASE_URL="$DB" -e USER_AGENT="$UA" "$IMAGE" registerwatch migrate > "$TMP/migrate" 2>&1; then
    echo "FAIL setup: registerwatch migrate against $DB"; sed 's/^/  | /' "$TMP/migrate" | tail -5; exit 1
  fi
  helper -v "$ROOT/tests/fixtures/registers:/fixtures:ro" -- net_probe.py fixtures /fixtures > "$TMP/fixtures" \
    || { echo "FAIL setup: loading fixtures"; exit 1; }
  helper -- os_probe.py seed --rows 5000 > "$TMP/seed" || { echo "FAIL setup: seeding gb_ukgc"; exit 1; }
  echo "setup: migrated; fixtures $(py 'd' "$TMP/fixtures"); gb_ukgc licences $(py 'd["current"]' "$TMP/seed") current; image $IMAGE"
fi

BASE="http://127.0.0.1:$PORT"
if wanted T10.1.d T10.3.a T10.3.b T10.3.c T10.4 T10.5.a; then
  api rw-net "$PORT" || fail setup "rw-net did not start"
fi

# --- P10.1 security headers at the box (T10.1.d) -------------------------------------

if wanted T10.1.d; then
  c -D "$TMP/h" -o /dev/null "$BASE$ROWS?limit=5"
  c -D "$TMP/h_https" -o /dev/null -H 'X-Forwarded-Proto: https' "$BASE$ROWS?limit=5"
  nosniff=$(header x-content-type-options "$TMP/h"); referrer=$(header referrer-policy "$TMP/h")
  csp=$(header content-security-policy "$TMP/h"); hsts_http=$(header strict-transport-security "$TMP/h")
  hsts_https=$(header strict-transport-security "$TMP/h_https")
  docs="node with Playwright not found"
  docs_ok=no
  if NODE_PATH=$(npm root -g 2>/dev/null) node -e 'require("playwright")' 2>/dev/null; then
    NODE_PATH=$(npm root -g) node "$ROOT/scripts/verify/docs_render.mjs" "$BASE" ${RW_DOCS_MIRROR:+--mirror "$RW_DOCS_MIRROR"} > "$TMP/docs"
    docs_code=$?
    [ $docs_code = 0 ] && docs_ok=ok
    docs=$(py '"; ".join("%s %s, %s operations drawn in %s ms, %d policy violations%s" % (l["page"], l["status"], l["operations"], l["ms"], len(l["violations"]), (" (" + ", ".join(l["violations"][:3]) + ")") if l["violations"] else "") for l in lines)' "$TMP/docs")
  fi
  check T10.1.d "$([ "$(status "$TMP/h")" = 200 ] && [ "$nosniff" = nosniff ] && [ "$referrer" = no-referrer ] \
      && [ "$csp" = "default-src 'none'; frame-ancestors 'none'" ] && [ -z "$hsts_http" ] && [ "$hsts_https" = max-age=31536000 ] && [ $docs_ok = ok ] && echo ok)" \
    "rows JSON $(status "$TMP/h"): nosniff=$nosniff, referrer-policy=$referrer, CSP \"$csp\"; HSTS over http: ${hsts_http:-none}, with X-Forwarded-Proto: https from the trusted 127.0.0.1: $hsts_https; Chromium: $docs"
fi

# --- P10.2 client address and proxy trust (T10.2.a, T10.2.b) -------------------------

# limited NAME PORT FORWARDED_ALLOW_IPS: 5 reads a minute; spend one address's
# five, then ask as it, as another address, and with no header.
limited() {
  api "$1" "$2" -e FORWARDED_ALLOW_IPS="$3" -e RATE_LIMIT_READ_PER_MIN=5 || return 1
  local i out=""
  for i in 1 2 3 4 5 6; do out+="$(c -o /dev/null -w '%{http_code}' -H 'X-Forwarded-For: 203.0.113.9' "http://127.0.0.1:$2/v1/jurisdictions") "; done
  echo "$out|$(c -o /dev/null -w '%{http_code}' -H 'X-Forwarded-For: 203.0.113.10' "http://127.0.0.1:$2/v1/jurisdictions")|$(c -o /dev/null -w '%{http_code}' "http://127.0.0.1:$2/v1/jurisdictions")|$(docker logs "$1" 2>&1 | grep -m1 'GET /v1/jurisdictions' | sed -E 's/^INFO: +//; s/ - .*//')"
}

if wanted T10.2.a; then
  IFS='|' read -r spoofed other plain logged <<<"$(limited rw-net-xff $((PORT + 2)) 127.0.0.1)"
  gone rw-net-xff
  check T10.2.a "$([ "$spoofed" = "200 200 200 200 200 429 " ] && [ "$other" = 200 ] && [ "$plain" = 200 ] && [ "$logged" = 203.0.113.9:0 ] && echo ok)" \
    "FORWARDED_ALLOW_IPS=127.0.0.1, 5 reads a minute, from 127.0.0.1: as X-Forwarded-For 203.0.113.9 → ${spoofed% }; then 203.0.113.10 → $other, no header (127.0.0.1) → $plain: one bucket per forwarded address. Access log client: $logged"
fi

if wanted T10.2.b; then
  IFS='|' read -r spoofed other plain logged <<<"$(limited rw-net-xff-untrusted $((PORT + 3)) 10.255.255.1)"
  gone rw-net-xff-untrusted
  check T10.2.b "$([ "$spoofed" = "200 200 200 200 200 429 " ] && [ "$other" = 429 ] && [ "$plain" = 429 ] && [[ "$logged" == 127.0.0.1:* ]] && echo ok)" \
    "FORWARDED_ALLOW_IPS=10.255.255.1, from 127.0.0.1: as 203.0.113.9 → ${spoofed% }; then 203.0.113.10 → $other, no header → $plain: the header is ignored, every request is 127.0.0.1's. Access log client: $logged"
fi

# --- P10.3 connection reuse and timeouts (T10.3.a–c) ---------------------------------

if wanted T10.3.b; then  # 80 s of waiting: start it now, collect it below
  helper -- net_probe.py idle "$BASE/livez" --sleep 60 --sleep 80 > "$TMP/idle" 2>&1 &
  idle_pid=$!
fi

if wanted T10.3.a; then
  c -v -o /dev/null -o /dev/null "$BASE/livez" "$BASE/livez" 2> "$TMP/reuse"
  reused=$(grep -c 'Re-using existing connection' "$TMP/reuse"); connects=$(grep -c 'Connected to' "$TMP/reuse")
  keep=$(c -D - -o /dev/null "$BASE/livez" | tr -d '\r' | grep -i '^connection:' || true)
  check T10.3.a "$([ "$reused" -ge 1 ] && [ "$connects" = 1 ] && echo ok)" \
    "curl -v \$RW_BASE/livez \$RW_BASE/livez: 'Re-using existing connection' $reused×, 'Connected to' $connects× (${keep:-no Connection header: HTTP/1.1 keep-alive})"
fi

if wanted T10.3.c; then
  helper -- net_probe.py slowloris "$BASE/livez" --clients 20 --every 1 --give-up 40 > "$TMP/slow"
  verdict=$(py '"ok" if d["closed_by"] == {"server": 20} and d["answers"] == {"HTTP/1.1 408 Request Timeout": 20} and d["after_s_max"] <= 30 and d["livez"]["failed"] == 0 and d["livez"]["max_ms"] < 50 else "no"' "$TMP/slow")
  check T10.3.c "$verdict" "$(py '"20 clients sending a %d-byte head a byte a second (giving up at 40 s): closed by %s after %s–%s s (bound 30 s), %d bytes sent at most, answers %s; meanwhile /livez on fresh connections %d times, %d failed, p50 %s ms, max %s ms (bound 50 ms)" % (d["head_bytes"], d["closed_by"], d["after_s_min"], d["after_s_max"], d["bytes_sent_max"], d["answers"], d["livez"]["n"], d["livez"]["failed"], d["livez"]["p50_ms"], d["livez"]["max_ms"])' "$TMP/slow")"
fi

if wanted T10.3.b; then
  wait "$idle_pid"
  verdict=$(py '"ok" if lines[0]["idle_s"] == 60 and lines[0]["second"] == 200 and lines[0]["same_connection"] and lines[1]["second"] != 200 else "no"' "$TMP/idle")
  check T10.3.b "$verdict" "$(py '"; ".join("request, %g s idle, request on the same socket → %s%s" % (l["idle_s"], l["second"], " (same connection)" if l["same_connection"] else " (the server had closed it)") for l in lines) + "; keep-alive 75 s"' "$TMP/idle")"
fi

# --- P10.4 payload efficiency (T10.4.a–c) --------------------------------------------

if wanted T10.4.a; then
  plain=$(c -o "$TMP/plain.json" -w '%{size_download}' -H 'Accept-Encoding: identity' "$BASE$ROWS?limit=1000")
  zipped=$(c -D "$TMP/hz" -o "$TMP/zipped.gz" -w '%{size_download}' -H 'Accept-Encoding: gzip' "$BASE$ROWS?limit=1000")
  rows=$(py 'len(json.load(open(sys.argv[2]))["data"])' "$TMP/plain.json")
  same=$(python3 -c "import gzip, sys
try: print(gzip.decompress(open(sys.argv[1], 'rb').read()) == open(sys.argv[2], 'rb').read())
except OSError as e: print(e)" "$TMP/zipped.gz" "$TMP/plain.json")
  ratio=$(awk -v z="$zipped" -v p="$plain" 'BEGIN { printf "%.1f", z / p * 100 }')
  check T10.4.a "$([ "$rows" = 1000 ] && [ "$(header content-encoding "$TMP/hz")" = gzip ] && [ "$same" = True ] && awk -v r="$ratio" 'BEGIN { exit !(r <= 25) }' && echo ok)" \
    "1000-row page: $plain bytes plain, $zipped gzipped ($ratio %, bound 25 %); content-encoding $(header content-encoding "$TMP/hz"), decompresses to the same bytes: $same; Vary: $(header vary "$TMP/hz")"
fi

if wanted T10.4.b; then
  small=""; ok=ok
  for p in /livez /v1/registers/xx_nope "$ROWS?limit=1"; do
    size=$(c -D "$TMP/hs" -o /dev/null -w '%{size_download}' -H 'Accept-Encoding: gzip' "$BASE$p")
    enc=$(header content-encoding "$TMP/hs")
    small+="$p $(status "$TMP/hs") ${size} B ${enc:-identity}; "
    { [ "$size" -lt 1024 ] && [ -z "$enc" ]; } || ok=no
  done
  check T10.4.b "$ok" "with Accept-Encoding: gzip: ${small%; }"
fi

if wanted T10.4.c; then
  helper -- net_probe.py oversize "$BASE/v1/ingest-runs" --bytes 2097152 --token "$TOKEN" > "$TMP/big"
  code=$(c -o "$TMP/big100" -w '%{http_code} %{size_upload} %{time_total}' -H "Authorization: Bearer $TOKEN" \
           -H 'Content-Type: application/json' --data-binary @<(head -c 2097152 /dev/zero | tr '\0' ' ') "$BASE/v1/ingest-runs")
  verdict=$(py '"ok" if d["status_line"].startswith("HTTP/1.1 413") and d["body"]["type"].endswith("#content-too-large") and "connection: close" in [h.lower() for h in d["headers"]] and d["answered_ms"] < 500 and d["closed_ms"] is not None and d["closed_ms"] < 3000 else "no"' "$TMP/big")
  check T10.4.c "$([ "$verdict" = ok ] && [[ "$code" == 413\ 0\ * ]] && echo ok)" \
    "$(py '"2 MiB declared, sent without waiting: %s (%s) in %s ms after %d bytes of body, connection closed by the server at %s ms" % (d["status_line"], d["body"]["type"].rsplit("#", 1)[1], d["answered_ms"], d["body_sent"], d["closed_ms"])' "$TMP/big"); curl (Expect: 100-continue): status, bytes of body sent, seconds: $code"
fi

# --- P10.5 CORS (T10.5.a, T10.5.b) ---------------------------------------------------

preflight() {  # preflight BASE ORIGIN METHOD: status and the CORS answer
  c -D "$TMP/pf" -o /dev/null -X OPTIONS -H "Origin: $2" -H "Access-Control-Request-Method: $3" \
    -H 'Access-Control-Request-Headers: authorization' "$1$ROWS"
  echo "$(status "$TMP/pf") allow-origin=$(header access-control-allow-origin "$TMP/pf") allow-methods=$(header access-control-allow-methods "$TMP/pf")"
}

if wanted T10.5.a; then
  pf=$(preflight "$BASE" https://evil.test GET)
  simple=$(c -D - -o /dev/null -H 'Origin: https://evil.test' "$BASE$ROWS?limit=1" | tr -d '\r' | grep -ci '^access-control-allow-origin' || true)
  check T10.5.a "$([[ "$pf" == *"allow-origin= "* ]] && [ "$simple" = 0 ] && echo ok)" \
    "CORS_ALLOW_ORIGINS unset: preflight from https://evil.test → $pf; a GET from it has no access-control-allow-origin ($simple)"
fi

if wanted T10.5.b; then
  cors="http://127.0.0.1:$((PORT + 1))"
  api rw-net-cors $((PORT + 1)) -e CORS_ALLOW_ORIGINS=https://app.test
  get=$(preflight "$cors" https://app.test GET); post=$(preflight "$cors" https://app.test POST)
  other=$(preflight "$cors" https://evil.test GET)
  read_back=$(c -D - -o /dev/null -H 'Origin: https://app.test' "$cors$ROWS?limit=1" | tr -d '\r' | grep -i '^access-control-allow-origin' | sed 's/^[^:]*: //')
  gone rw-net-cors
  check T10.5.b "$([[ "$get" == "200 allow-origin=https://app.test allow-methods=GET, HEAD" ]] && [[ "$post" == 400\ * ]] && [[ "$post" != *POST* ]] && [[ "$other" == *"allow-origin= "* ]] && [ "$read_back" = https://app.test ] && echo ok)" \
    "CORS_ALLOW_ORIGINS=https://app.test: preflight GET → $get; preflight POST → $post; preflight GET from https://evil.test → $other; GET from the origin → allow-origin $read_back"
fi

gone rw-net

# --- P10.6 load and latency (T10.6.a, T10.6.b) ---------------------------------------

if wanted T10.6.a; then
  # One client address can read 600 a minute and search 60: the load comes from
  # 40 addresses (X-Forwarded-For, from 127.0.0.1, which the server trusts), each
  # at 2.5 requests a second, under both limits.
  api rw-net-load $((PORT + 4)) -e WEB_CONCURRENCY="$WORKERS"
  helper -- load.py --base "http://127.0.0.1:$((PORT + 4))" --rps 100 --duration "$LOAD_S" --clients 40 "${MIX[@]}" > "$TMP/load"
  gone rw-net-load
  verdict=$(py '"ok" if d["p95_ms"] < 250 and d["p99_ms"] < 800 and not d["errors"] and not any(s.startswith("5") for s in d["statuses"]) and set(d["statuses"]) == {"200"} else "no"' "$TMP/load")
  check T10.6.a "$verdict" "$(py '"100 rps for %ss, WEB_CONCURRENCY='"$WORKERS"', 40 client addresses: %s, errors %s, p50 %s, p95 %s, p99 %s, max %s ms (bounds p95 250, p99 800); took %s s; by class %s" % (d["duration_s"], d["statuses"], d["errors"], d["p50_ms"], d["p95_ms"], d["p99_ms"], d["max_ms"], d["elapsed_s"], {k: "%s %s/%s/%s ms" % (v["sent"], v["p50_ms"], v["p95_ms"], v["p99_ms"]) for k, v in d["classes"].items()})' "$TMP/load")"
fi

if wanted T10.6.b; then
  # One read token at three times its limit (600 a minute: 30 a second) for a minute,
  # on one worker, so its one bucket is the whole count: 600 at once, then 10 a second.
  api rw-net-limit $((PORT + 5)) -e READ_TOKEN="$READ" -e WEB_CONCURRENCY=1
  helper -- net_probe.py poll "http://127.0.0.1:$((PORT + 5))/readyz" --every 0.5 --seconds 62 > "$TMP/ready" &
  rp=$!
  helper -- load.py --base "http://127.0.0.1:$((PORT + 5))" --rps 30 --duration 60 --token "$READ" --path "$ROWS" > "$TMP/over"
  wait $rp
  for i in $(seq 100); do  # spend what refilled meanwhile, and show the refusal
    c -D "$TMP/h429" -o /dev/null -H "Authorization: Bearer $READ" "http://127.0.0.1:$((PORT + 5))$ROWS"
    [ "$(status "$TMP/h429")" = 429 ] && break
  done
  retry="$(status "$TMP/h429") $(tr -d '\r' < "$TMP/h429" | grep -iE '^(content-type|retry-after|ratelimit):' | tr '\n' ' ')"
  gone rw-net-limit
  verdict=$(py '"ok" if set(d["statuses"]) <= {"200", "429"} and not d["errors"] and 1150 <= d["statuses"].get("200", 0) <= 1260 else "no"' "$TMP/over")
  check T10.6.b "$([ "$verdict" = ok ] && [ "$(py 'list(d["statuses"])' "$TMP/ready")" = "['200']" ] && echo ok)" \
    "$(py '"30 rps for 60 s on one read token (limit 600/min): %s, errors %s, p95 %s ms (expected about 600 + 10/s × 60 s = 1,200 × 200, the rest 429)" % (d["statuses"], d["errors"], d["p95_ms"])' "$TMP/over"); /readyz every 0.5 s meanwhile: $(py 'str(d["statuses"]) + ", slowest %s ms" % d["max_ms"]' "$TMP/ready"); a 429 carries: $retry"
fi

# --- [edge]: TLS, redirect, certificate, HTTP/2, latency (T10.1.a–c, T10.3.d, T10.6.c) --

if wanted T10.1.a T10.1.b T10.1.c T10.2.c T10.3.d T10.6.c; then
  if [ -n "${RW_EDGE:-}" ]; then
    EDGE="$RW_EDGE"; HOST=$(python3 -c 'import sys, urllib.parse as u; print(u.urlsplit(sys.argv[1]).hostname)' "$RW_EDGE")
    ADDR="$HOST"; RESOLVE=(); CA_OPTS=(); WHERE="staging $EDGE"
  else
    HOST="$EDGE_HOST"; EDGE="https://$HOST"; ADDR=127.0.0.1; WHERE="local edge (Caddy, $EDGE)"
    docker network create --subnet "$SUBNET" "$NET" >/dev/null 2>&1
    # The app behind it reaches Postgres through the host; it believes X-Forwarded-*
    # from the edge's address only. Limits raised for T10.6.c, whose load is one client.
    edge_db=$(python3 -c 'import sys, urllib.parse as u; p = u.urlsplit(sys.argv[1]); print(p._replace(netloc=p.netloc.replace(p.hostname, "host.docker.internal")).geturl())' "$DB")
    docker run -d --name rw-edge-app --network "$NET" --ip "$APP_IP" --read-only --tmpfs /tmp \
      --add-host host.docker.internal:host-gateway -e DATABASE_URL="$edge_db" -e USER_AGENT="$UA" \
      -e BLOB_BACKEND=local -e SNAPSHOT_ROOT=/tmp/snapshots -e FORWARDED_ALLOW_IPS="$EDGE_IP" \
      -e RATE_LIMIT_READ_PER_MIN=6000 -e RATE_LIMIT_SEARCH_PER_MIN=6000 "$IMAGE" >/dev/null
    docker run -d --name rw-edge --network "$NET" --ip "$EDGE_IP" -p 127.0.0.1:80:80 -p 127.0.0.1:443:443 \
      -e RW_EDGE_HOST="$HOST" -e RW_EDGE_UPSTREAM="$APP_IP:8000" \
      -v "$ROOT/scripts/verify/edge/Caddyfile:/etc/caddy/Caddyfile:ro" mirror.gcr.io/library/caddy:2-alpine >/dev/null
    RESOLVE=(--resolve "$HOST:443:127.0.0.1" --resolve "$HOST:80:127.0.0.1")
    for i in $(seq 100); do docker exec rw-edge test -f /data/caddy/pki/authorities/local/root.crt 2>/dev/null && break; sleep 0.2; done
    docker cp rw-edge:/data/caddy/pki/authorities/local/root.crt "$TMP/edge-root.crt" >/dev/null
    # The system's store, plus the stand-in's own root: what a client trusts for a public certificate.
    cat /etc/ssl/certs/ca-certificates.crt "$TMP/edge-root.crt" > "$TMP/edge-store.crt"
    CA_OPTS=(--cacert "$TMP/edge-store.crt")
    export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$HOST" no_proxy="${no_proxy:+$no_proxy,}$HOST"
    for i in $(seq 100); do curl -fsS -o /dev/null "${RESOLVE[@]}" "${CA_OPTS[@]}" "$EDGE/livez" 2>/dev/null && break; sleep 0.2; done
  fi
  e() { curl -sS "${RESOLVE[@]}" "${CA_OPTS[@]}" "$@"; }
fi

if wanted T10.1.a; then
  tls() {  # tls VERSION: what the handshake came to (the client allows old versions: SECLEVEL=0)
    timeout 15 openssl s_client -connect "$ADDR:443" -servername "$HOST" "-$1" -cipher 'DEFAULT:@SECLEVEL=0' </dev/null > "$TMP/tls" 2>&1
    if grep -q '^New, TLSv1\.[23]\|^New, TLSv1\.1\|^New, TLSv1,' "$TMP/tls" && ! grep -q 'Cipher is (NONE)' "$TMP/tls"; then
      echo "$(grep -m1 '^New, ' "$TMP/tls" | sed 's/^New, //; s/, Cipher is / /')"
    else echo "refused ($(grep -o 'alert protocol version\|alert handshake failure\|no protocols available\|wrong version number' "$TMP/tls" | head -1 || true))"; fi
  }
  v11=$(tls tls1_1); v12=$(tls tls1_2); v13=$(tls tls1_3)
  check T10.1.a "$([[ "$v11" == "refused (alert protocol version)" ]] && [[ "$v12" == TLSv1.2* ]] && [[ "$v13" == TLSv1.3* ]] && echo ok)" \
    "$WHERE: -tls1_1 $v11; -tls1_2 $v12; -tls1_3 $v13"
fi

if wanted T10.1.b; then
  redirect=$(curl -sS "${RESOLVE[@]}" -o /dev/null -w '%{http_code} %{redirect_url}' "http://$HOST/livez")
  e -D "$TMP/edge_h" -o /dev/null "$EDGE/livez"
  hsts=$(header strict-transport-security "$TMP/edge_h")
  check T10.1.b "$([[ "$redirect" == 30[18]\ https://$HOST/livez ]] && [ "$hsts" = max-age=31536000 ] && echo ok)" \
    "$WHERE: http://$HOST/livez → $redirect; https → $(status "$TMP/edge_h") with strict-transport-security: $hsts"
fi

if wanted T10.1.c; then
  e --fail -o /dev/null "$EDGE/livez"; code=$?
  issuer=$(timeout 15 openssl s_client -connect "$ADDR:443" -servername "$HOST" </dev/null 2>/dev/null | openssl x509 -noout -issuer -enddate 2>/dev/null | tr '\n' ' ')
  bare=$(curl -sS --noproxy '*' "${RESOLVE[@]}" --cacert /etc/ssl/certs/ca-certificates.crt -o /dev/null "$EDGE/livez" 2>&1 | head -1)
  check T10.1.c "$([ $code = 0 ] && echo ok)" \
    "$WHERE: curl --fail $EDGE/livez without -k → exit $code; certificate $issuer${CA_OPTS:+; trusting the system store alone: ${bare:-verified}}"
fi

if wanted T10.2.c; then
  if [ -n "${RW_EDGE:-}" ]; then
    echo "SKIP T10.2.c: needs the app's access log (local edge only)"
  else
    e -o /dev/null -H 'X-Forwarded-For: 203.0.113.9' "$EDGE/v1/jurisdictions/zz-forwarded-for"
    logged=$(docker logs rw-edge-app 2>&1 | grep 'zz-forwarded-for' | tail -1 | sed -E 's/^INFO: +//; s/ - .*//')
    gateway=$(docker network inspect "$NET" -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}')
    check T10.2.c "$([[ "$logged" == "$gateway:"* ]] && echo ok)" \
      "a client sending X-Forwarded-For: 203.0.113.9 through the edge: the app (FORWARDED_ALLOW_IPS=$EDGE_IP, the edge) logs it as $logged, its real address as the edge saw it ($gateway, the Docker gateway), not the header"
  fi
fi

if wanted T10.3.d; then
  urls=(); for i in $(seq 200); do urls+=(-o /dev/null "$EDGE/livez"); done
  e --http2 -w '%{http_version} %{http_code} %{num_connects}\n' "${urls[@]}" > "$TMP/h2"; code=$?
  summary=$(awk '{ v[$1]++; s[$2]++; n += $3 } END { for (k in v) printf "http_version %s ×%d, ", k, v[k]; for (k in s) printf "status %s ×%d, ", k, s[k]; printf "%d connection(s)", n }' "$TMP/h2")
  check T10.3.d "$([ $code = 0 ] && [ "$(grep -c '^2 200 ' "$TMP/h2")" = 200 ] && [ "$(awk '{ n += $3 } END { print n }' "$TMP/h2")" = 1 ] && echo ok)" \
    "$WHERE: 200 sequential GETs in one curl --http2: $summary; curl exit $code (no resets)"
fi

if wanted T10.6.c; then
  if [ -n "${RW_EDGE:-}" ]; then edge_net=(); cacert=(); else
    edge_net=(--add-host "$HOST:127.0.0.1" -v "$TMP/edge-store.crt:/edge-store.crt:ro"); cacert=(--cacert /edge-store.crt); fi
  helper "${edge_net[@]}" -- load.py --base "$EDGE" --rps 20 --duration 60 "${cacert[@]}" "${MIX[@]}" > "$TMP/edge_load"
  verdict=$(py '"ok" if d["p95_ms"] < 400 and not d["errors"] and set(d["statuses"]) == {"200"} else "no"' "$TMP/edge_load")
  check T10.6.c "$verdict" "$(py '"'"$WHERE"': 20 rps for 60 s over https, T10.6.a mix: %s, errors %s, p50 %s, p95 %s, p99 %s, max %s ms (bound p95 400)" % (d["statuses"], d["errors"], d["p50_ms"], d["p95_ms"], d["p99_ms"], d["max_ms"])' "$TMP/edge_load")"
fi

exit $FAILED
