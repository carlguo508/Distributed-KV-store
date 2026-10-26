#!/usr/bin/env bash
# Run one checkpoint experiment with N=3 diagnostic servers on ports 7400-7402 and fresh data.
# usage: spike/checkpoint/run_test.sh <T1|T3|T4> <run number>
#   T1  prefill, then the 60 s timed run right away (baseline)
#   T3  prefill, stop servers, manual TRUNCATE checkpoint, restart, 60 s timed run
#   T4  as T3, but the restarted servers run with wal_autocheckpoint=0 (no checkpoints at all
#       during the timed run; the WAL grows instead). Diagnostic only, not a configuration.
set -euo pipefail
TEST=$1; RUN=$2
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
N=3; SECONDS_TIMED=${SECS:-60}; KEYS=${KEYS:-100000}   # env overrides are for smoke tests only
NAME="$TEST-r$RUN"
DATA="$HERE/data/$NAME"            # raw data and server logs (git-ignored)
RES="$HERE/results"                # per-second tables and summaries (committed)
rm -rf "$DATA"; mkdir -p "$DATA" "$RES"

"$PY" - "$N" "$DATA" <<'EOF'
import json, sys
n, d = int(sys.argv[1]), sys.argv[2]
json.dump({"servers": [{"id": i, "host": "127.0.0.1", "port": 7400 + i, "data_dir": f"{d}/s{i}"}
                       for i in range(n)], "max_batch": 256}, open(f"{d}/cluster.json", "w"))
EOF
CFG="$DATA/cluster.json"

PIDS=()
start_servers() {   # $1 = log phase name, rest = extra PRAGMAs
  local phase=$1; shift
  PIDS=()
  for i in $(seq 0 $((N - 1))); do
    "$PY" "$HERE/diag_server.py" "$CFG" "$i" "$DATA/s$i-$phase.commits.csv" "$@" 2>>"$DATA/s$i.log" &
    PIDS+=($!)
  done
  for i in $(seq 0 $((N - 1))); do
    for _ in $(seq 1 200); do nc -z 127.0.0.1 $((7400 + i)) 2>/dev/null && break; sleep 0.1; done
    nc -z 127.0.0.1 $((7400 + i)) || { echo "server $i did not start; see $DATA/s$i.log" >&2; exit 1; }
  done
}
stop_servers() {
  kill -TERM "${PIDS[@]}"; wait "${PIDS[@]}" 2>/dev/null || true
}
bench() { (cd "$ROOT" && "$PY" -m kvstore.bench --config "$CFG" --keys "$KEYS" --seed "$RUN" "$@"); }

echo "=== $NAME $(date '+%F %T')  load: $(uptime | sed 's/.*load averages*: //')"
case "$TEST" in
  T1)
    start_servers timed
    bench --seconds "$SECONDS_TIMED" --per-second --out "$RES/$NAME.csv" || true
    stop_servers
    ;;
  BASE)  # same as T1; used with SECS=20 for the background-checkpoint comparison
    start_servers timed
    bench --seconds "$SECONDS_TIMED" --per-second --out "$RES/$NAME.csv" || true
    stop_servers
    ;;
  BG)    # candidate fix: no checkpoint inside COMMIT, PASSIVE checkpoint every 1 s in a thread
    BG_CKPT=1 start_servers timed wal_autocheckpoint=0
    bench --seconds "$SECONDS_TIMED" --per-second --out "$RES/$NAME.csv" || true
    stop_servers
    ;;
  T3|T4)
    start_servers prefill
    bench --seconds 0 | grep '^prefill' || true
    stop_servers
    "$PY" "$HERE/checkpoint.py" "$DATA"/s*/kv.db
    if [ "$TEST" = T4 ]; then start_servers timed wal_autocheckpoint=0; else start_servers timed; fi
    bench --no-prefill --seconds "$SECONDS_TIMED" --per-second --out "$RES/$NAME.csv" || true
    stop_servers
    ;;
  *) echo "unknown test $TEST" >&2; exit 2 ;;
esac
if grep -h "commit failed" "$DATA"/s*.log; then echo "!!! a server failed a commit" >&2; fi
"$PY" "$HERE/analyze.py" "$HERE" "$NAME" | tee "$RES/$NAME.summary.txt"
