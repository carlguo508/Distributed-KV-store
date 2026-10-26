#!/usr/bin/env bash
# Start every server listed in a cluster config, in the background (design.md §6 Scripts).
# usage: scripts/start.sh [cluster.json]
set -euo pipefail
CONFIG=${1:-cluster.json}
cd "$(dirname "$0")/.."
# PYTHON can be overridden so tests run the servers with the same interpreter as pytest.
PY=${PYTHON:-.venv/bin/python}
# Seconds to wait for each server's port before giving up; overridable for tests.
START_TIMEOUT=${START_TIMEOUT:-20}

# The config is parsed with Python rather than jq, so the only dependency is the venv.
N=$("$PY" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["servers"]))' "$CONFIG")
for i in $(seq 0 $((N - 1))); do
  read -r HOST PORT DIR < <("$PY" -c '
import json, sys
s = json.load(open(sys.argv[1]))["servers"][int(sys.argv[2])]
print(s["host"], s["port"], s["data_dir"])' "$CONFIG" "$i")
  mkdir -p "$DIR"
  # Idempotent: a second start.sh does not launch a duplicate server on the same data dir,
  # which would mean two writers on one kv.db.
  if [ -f "$DIR/kvserver.pid" ] && kill -0 "$(cat "$DIR/kvserver.pid")" 2>/dev/null; then
    echo "server $i already running (pid $(cat "$DIR/kvserver.pid"))"; continue
  fi
  "$PY" -m kvstore.server --config "$CONFIG" --id "$i" >>"$DIR/kvserver.log" 2>&1 &
  echo $! >"$DIR/kvserver.pid"
  # Wait until the port accepts a TCP connection. The server opens its port only after
  # loading every committed key (design.md §7 step 4), so once this succeeds the server is
  # ready to serve. Loading a large dataset takes time, hence a generous default.
  PID=$(cat "$DIR/kvserver.pid")
  READY=0
  DEADLINE=$(( $(date +%s) + START_TIMEOUT ))
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if "$PY" -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 0.2).close()' "$HOST" "$PORT" 2>/dev/null; then
      READY=1; break
    fi
    sleep 0.1
  done
  if [ "$READY" -ne 1 ]; then
    # Never report success for a server that is not serving. Kill a possibly hung process
    # and drop its pid file so a later start.sh does not mistake it for a running server.
    kill -TERM "$PID" 2>/dev/null || true
    rm -f "$DIR/kvserver.pid"
    echo "error: server $i not listening on $HOST:$PORT after ${START_TIMEOUT}s; see $DIR/kvserver.log" >&2
    exit 1
  fi
  echo "server $i listening on $HOST:$PORT (pid $PID)"
done
