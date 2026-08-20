#!/usr/bin/env bash
#
# Capture a comprehensive performance baseline for EvoAgent.
#
# Orchestrates scripts/loadgen.py and scripts/microbench.py across a fixed suite
# of scenarios and writes per-run JSON plus a machine-readable index to an output
# directory. Results are machine-specific and are not committed as a baseline.
#
# Suites:
#   single      steady rate-sweep (knee), intake, spike recovery, compressed soak
#   overload    rate-limit shed (429 + Retry-After) and heavy-gate shed (503) + recovery
#   micro       hot-path micro-benchmarks
#   multiworker web-layer horizontal scaling via SO_REUSEPORT workers
#   all         everything above (default)
#
# Usage:
#   scripts/perf_baseline.sh [suite] [--out DIR] [--quick]
#
# Options:
#   --out DIR    output directory (default: perf/baseline-<timestamp>)
#   --quick      shorter durations for a smoke of the harness itself
#   -h, --help   show this help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${PYTHON:-${PROJECT_DIR}/.venv/bin/python}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON="python3"
: "${EVOAGENT_DATABASE_URL:?set EVOAGENT_DATABASE_URL to a disposable PostgreSQL database}"

SUITE="all"
OUT=""
QUICK=0
PORT="${EVOAGENT_PERF_PORT:-8199}"

while [ $# -gt 0 ]; do
  case "$1" in
    single|overload|micro|multiworker|all) SUITE="$1" ;;
    --out) shift; OUT="$1" ;;
    --quick) QUICK=1 ;;
    -h|--help) sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -n "$OUT" ] || OUT="${PROJECT_DIR}/perf/baseline-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

# Durations (seconds). --quick shrinks them for a self-test.
if [ "$QUICK" -eq 1 ]; then
  WARMUP=1; STEADY_DUR=5; SPIKE_DUR=6; SOAK_DUR=15; SWEEP="200 500"; OVERLOAD_DUR=6
else
  WARMUP=3; STEADY_DUR=20; SPIKE_DUR=20; SOAK_DUR=180
  SWEEP="300 1000 1500 2000 3000 4000"; OVERLOAD_DUR=15
fi

BASE_URL="http://127.0.0.1:${PORT}"
SERVER_PID=""
SERVER_LOG="${OUT}/server.log"

info() { printf "\033[34m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[33m!\033[0m %s\n" "$*"; }

# --- server lifecycle -------------------------------------------------------
# The server always enables SO_REUSEPORT, so a leaked/orphaned instance can keep
# sharing $PORT and silently pollute results. Guarantee the port is empty by
# killing anything still listening on it.
free_port() {
  local pids i=1
  while [ "$i" -le 20 ]; do
    pids=$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)
    [ -z "$pids" ] && return 0
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
    sleep 0.2; i=$((i + 1))
  done
}

boot_server() {
  # Args: label + KEY=VALUE env overrides. Boots a fresh server on $PORT.
  local label="$1"; shift
  free_port
  info "Booting server [${label}] on :${PORT} ($*)"
  # `exec` replaces the subshell with the interpreter so $! is the real Python
  # process (not a throwaway shell), making teardown reliable.
  (
    cd "$PROJECT_DIR"
    exec env EVOAGENT_HOST=127.0.0.1 EVOAGENT_PORT="$PORT" EVOAGENT_AUTH_REQUIRED=false \
        EVOAGENT_DATABASE_URL="$EVOAGENT_DATABASE_URL" \
        "$@" "$PYTHON" -m evoagent
  ) >>"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  local i=1
  while [ "$i" -le 40 ]; do
    if curl -fsS "${BASE_URL}/health" >/dev/null 2>&1; then
      ok "server ready (pid ${SERVER_PID})"; return 0
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      warn "server process died; see ${SERVER_LOG}"; return 1
    fi
    sleep 0.5; i=$((i + 1))
  done
  warn "server did not become ready"; return 1
}

stop_server() {
  [ -n "$SERVER_PID" ] || return 0
  # Capture workers BEFORE killing the parent (once the parent dies they are
  # reparented and pgrep -P can no longer find them).
  local kids; kids=$(pgrep -P "$SERVER_PID" 2>/dev/null || true)
  # Kill the supervisor FIRST so it stops respawning crashed workers (the
  # multi-worker master auto-restarts children; killing workers first just makes
  # it spawn new ones that keep the SO_REUSEPORT socket alive forever).
  kill "$SERVER_PID" 2>/dev/null || true
  # shellcheck disable=SC2086
  [ -n "$kids" ] && kill $kids 2>/dev/null || true
  local i=1
  while [ "$i" -le 10 ] && kill -0 "$SERVER_PID" 2>/dev/null; do sleep 0.3; i=$((i + 1)); done
  kill -9 "$SERVER_PID" 2>/dev/null || true
  # shellcheck disable=SC2086
  [ -n "$kids" ] && kill -9 $kids 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  # Belt and suspenders: nothing must survive on the port (SO_REUSEPORT).
  free_port
  SERVER_PID=""
}
trap stop_server EXIT

rss_kb() {
  # Total RSS (KB) of the server process tree (parent + forked workers).
  local total=0 r
  for pid in $(pgrep -P "$SERVER_PID" 2>/dev/null) "$SERVER_PID"; do
    r=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')
    [ -n "$r" ] && total=$((total + r))
  done
  echo "$total"
}

# --- loadgen wrapper --------------------------------------------------------
run_load() {
  # Args: name scenario rate [extra loadgen args...]
  # Writes loadgen text to <name>.txt and JSON to <name>.json; echoes ONLY the
  # json path on stdout so callers can capture it with command substitution.
  local name="$1" scenario="$2" rate="$3"; shift 3
  local json="${OUT}/${name}.json"
  "$PYTHON" "${PROJECT_DIR}/scripts/loadgen.py" \
    --base-url "$BASE_URL" --scenario "$scenario" \
    --duration "$STEADY_DUR" --rate "$rate" --warmup "$WARMUP" \
    --json "$json" "$@" > "${OUT}/${name}.txt" 2>&1 || true
  echo "$json"
}

json_field() {  # json_field FILE dotted.path
  "$PYTHON" - "$1" "$2" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
cur = data
for key in sys.argv[2].split('.'):
    cur = cur[key]
print(cur)
PY
}

# --- suites -----------------------------------------------------------------
suite_single() {
  boot_server single || return 1

  info "Rate sweep (breakpoint / knee) on read mix"
  : > "${OUT}/knee.csv"
  echo "rate,throughput_rps,p50,p95,p99,p99_9,max,error_rate" >> "${OUT}/knee.csv"
  local knee="none"
  for rate in $SWEEP; do
    local j; j=$(run_load "sweep-${rate}" steady "$rate")
    local tp p50 p95 p99 p999 mx er
    tp=$(json_field "$j" throughput_rps); p50=$(json_field "$j" latency_ms.p50)
    p95=$(json_field "$j" latency_ms.p95); p99=$(json_field "$j" latency_ms.p99)
    p999=$(json_field "$j" latency_ms.p99_9); mx=$(json_field "$j" latency_ms.max)
    er=$(json_field "$j" error_rate)
    echo "${rate},${tp},${p50},${p95},${p99},${p999},${mx},${er}" >> "${OUT}/knee.csv"
    # Knee = first rate where p99 > 150ms SLO or errors appear.
    if [ "$knee" = "none" ]; then
      local breach; breach=$("$PYTHON" -c "print(1 if (float('$p99')>150.0 or float('$er')>0.001) else 0)")
      [ "$breach" = "1" ] && knee="$rate"
    fi
  done
  echo "$knee" > "${OUT}/knee.value"
  ok "Knee (first SLO breach): ${knee} req/s"

  info "Async intake mix"
  run_load intake intake 200 >/dev/null

  info "Spike (burst + recovery)"
  "$PYTHON" "${PROJECT_DIR}/scripts/loadgen.py" --base-url "$BASE_URL" \
    --scenario spike --duration "$SPIKE_DUR" --rate 1500 --warmup 1 \
    --json "${OUT}/spike.json" | tee "${OUT}/spike.txt" || true

  info "Compressed soak (${SOAK_DUR}s @ 500 rps) - watching RSS for leaks"
  local rss_before rss_after
  rss_before=$(rss_kb)
  "$PYTHON" "${PROJECT_DIR}/scripts/loadgen.py" --base-url "$BASE_URL" \
    --scenario soak --duration "$SOAK_DUR" --rate 500 --warmup 2 \
    --json "${OUT}/soak.json" | tee "${OUT}/soak.txt" || true
  rss_after=$(rss_kb)
  curl -fsS "${BASE_URL}/metrics" > "${OUT}/soak-metrics.txt" 2>/dev/null || true
  printf 'rss_before_kb,rss_after_kb,delta_kb\n%s,%s,%s\n' \
    "$rss_before" "$rss_after" "$((rss_after - rss_before))" > "${OUT}/soak-rss.csv"
  ok "Soak RSS: ${rss_before}KB -> ${rss_after}KB (delta $((rss_after - rss_before))KB)"

  stop_server
}

# Concurrent burst against one endpoint. Counts HTTP statuses and records the
# Retry-After header seen on the first shed (429/503) response, proving the
# backpressure contract. Args: label method path body_json concurrency count.
burst() {
  local label="$1" method="$2" path="$3" body="$4" conc="$5" count="$6"
  "$PYTHON" - "$BASE_URL" "$method" "$path" "$body" "$conc" "$count" \
    "${OUT}/${label}.json" <<'PY' | tee "${OUT}/${label}.txt" || true
import concurrent.futures as cf, json, sys, urllib.request, urllib.error
base, method, path, body_json, conc, count, out = sys.argv[1:8]
conc, count = int(conc), int(count)
data = body_json.encode() if body_json and body_json != "-" else None
def hit(_):
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Retry-After")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Retry-After")
    except Exception:
        return 0, None
with cf.ThreadPoolExecutor(max_workers=conc) as ex:
    results = list(ex.map(hit, range(count)))
counts, retry_after = {}, None
for code, ra in results:
    counts[code] = counts.get(code, 0) + 1
    if retry_after is None and code in (429, 503) and ra is not None:
        retry_after = ra
summary = {
    "label": path, "method": method, "concurrency": conc, "total": count,
    "status_counts": {str(k): v for k, v in sorted(counts.items())},
    "shed_429": counts.get(429, 0), "shed_503": counts.get(503, 0),
    "ok_2xx": sum(v for k, v in counts.items() if 200 <= k < 300),
    "retry_after_seen": retry_after,
}
json.dump(summary, open(out, "w"), indent=2)
print(json.dumps(summary))
PY
}

suite_overload() {
  # --- A) Rate-limit shedding (429 + Retry-After) --------------------------
  # The limiter keys on client IP; from localhost every request shares one
  # bucket, so a low ceiling proves clean shedding. Probe paths
  # (/health,/ready,/metrics) bypass the limiter BY DESIGN. We drive the
  # open-model generator (sustained arrival) at the intake mix, whose POST
  # /v1/reviews?async=true is non-probe and rate-limited.
  boot_server ratelimit EVOAGENT_RATE_LIMIT_RPS=100 EVOAGENT_RATE_LIMIT_BURST=20 || return 1
  info "Rate-limit shed: intake @ 800 rps into a 100 rps ceiling (expect ~87% 429)"
  "$PYTHON" "${PROJECT_DIR}/scripts/loadgen.py" --base-url "$BASE_URL" \
    --scenario intake --duration "$OVERLOAD_DUR" --rate 800 --warmup 0 \
    --json "${OUT}/overload-ratelimit.json" | tee "${OUT}/overload-ratelimit.txt" || true
  info "Retry-After evidence (parallel probe to drain the bucket)"
  # Fire many requests in parallel so arrival momentarily exceeds the ceiling and
  # we capture a real shed response with its Retry-After header.
  seq 1 200 | xargs -P 64 -I{} curl -s -o /dev/null \
    -w "%{http_code}:%header{retry-after}\n" "${BASE_URL}/api/dashboard" \
    > "${OUT}/overload-retry-after.txt" 2>&1 || true
  if grep -m1 '^429:[0-9]' "${OUT}/overload-retry-after.txt" >/dev/null; then
    ok "shed responses carry Retry-After: $(grep -m1 '^429:' "${OUT}/overload-retry-after.txt")"
  else
    warn "no 429 captured in the Retry-After probe"
  fi

  info "Recovery: intake @ 40 rps under the 100 ceiling (expect ~0 shedding)"
  "$PYTHON" "${PROJECT_DIR}/scripts/loadgen.py" --base-url "$BASE_URL" \
    --scenario intake --duration "$OVERLOAD_DUR" --rate 40 --warmup 1 \
    --json "${OUT}/overload-recover.json" | tee "${OUT}/overload-recover.txt" || true
  stop_server

  # --- B) Heavy-gate shedding (503 + Retry-After) --------------------------
  # No rate limit here (so 429 does not mask the gate). Saturate the
  # bounded-concurrency gate with SYNC reviews (each runs the full multi-agent
  # graph, so requests stay in-flight long enough to overflow the gate).
  boot_server heavygate EVOAGENT_MAX_INFLIGHT_HEAVY=4 || return 1
  info "Heavy-gate 503: saturate sync POST /v1/reviews (gate=4) at concurrency 24"
  local rev_body
  rev_body='{"repository":"org/repo","diff":"--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,4 @@\n-x = eval(user)\n+import ast\n+x = ast.literal_eval(user)\n"}'
  burst overload-heavygate POST /v1/reviews "$rev_body" 24 96
  stop_server
}

suite_micro() {
  info "Micro-benchmarks (hot code paths)"
  (cd "$PROJECT_DIR" && "$PYTHON" scripts/microbench.py --json "${OUT}/microbench.json") \
    | tee "${OUT}/microbench.txt" || true
}

suite_multiworker() {
  local cores; cores=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)
  for w in 1 2 4; do
    boot_server "mw${w}" EVOAGENT_WEB_WORKERS="$w" || { warn "skip w=${w}"; continue; }
    info "Multi-worker w=${w}: read mix @ 3000 rps"
    "$PYTHON" "${PROJECT_DIR}/scripts/loadgen.py" --base-url "$BASE_URL" \
      --scenario steady --duration "$STEADY_DUR" --rate 3000 --concurrency 64 --warmup "$WARMUP" \
      --json "${OUT}/multiworker-w${w}.json" | tee "${OUT}/multiworker-w${w}.txt" || true
    stop_server
  done
}

# --- run --------------------------------------------------------------------
info "Output dir: ${OUT}"
"$PYTHON" --version 2>&1 | sed 's/^/python: /'
{
  echo "host: $(uname -srm)"
  echo "cores: $(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo '?')"
  echo "python: $($PYTHON --version 2>&1)"
  echo "os: $(sw_vers -productName 2>/dev/null) $(sw_vers -productVersion 2>/dev/null)"
  echo "suite: ${SUITE}"
  echo "quick: ${QUICK}"
  echo "timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${OUT}/environment.txt"
cat "${OUT}/environment.txt"

case "$SUITE" in
  single)      suite_single ;;
  overload)    suite_overload ;;
  micro)       suite_micro ;;
  multiworker) suite_multiworker ;;
  all)         suite_single; suite_overload; suite_micro; suite_multiworker ;;
esac

ok "Done. Artifacts in ${OUT}"
ls -1 "$OUT"
