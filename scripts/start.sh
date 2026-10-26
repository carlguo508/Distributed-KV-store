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
  # Preflight: the port must be free before launching. Otherwise the new server fails to
  # bind while the old listener (e.g. a leftover server from another config) answers for it.
  if "$PY" -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 0.2).close()' "$HOST" "$PORT" 2>/dev/null; then
    echo "error: $HOST:$PORT (server $i of $CONFIG) is already in use" >&2
    for HOLDER in $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true); do
      echo "  held by pid $HOLDER: $(ps -o command= -p "$HOLDER" 2>/dev/null || echo '?')" >&2
    done
    echo "fix: stop the holder (scripts/stop.sh <its config>, or kill <pid> if its pid file is gone)," >&2
    echo "     or give $CONFIG ports that no other cluster config uses" >&2
    exit 1
  fi
  "$PY" -m kvstore.server --config "$CONFIG" --id "$i" >>"$DIR/kvserver.log" 2>&1 &
  echo $! >"$DIR/kvserver.pid"
  # Wait until OUR process answers GET /info with the expected pid, id and n. The server
  # opens its port only after loading every committed key (design.md §7 step 4), so once
  # this succeeds the server is ready to serve. Loading a large dataset takes time, hence a
  # generous default. A bare TCP connect is not enough: another process may hold the port.
  PID=$(cat "$DIR/kvserver.pid")
  READY=0
  DEADLINE=$(( $(date +%s) + START_TIMEOUT ))
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if ! kill -0 "$PID" 2>/dev/null; then
      # Fail at once instead of waiting out START_TIMEOUT; the log says why (e.g. bind error).
      rm -f "$DIR/kvserver.pid"
      echo "error: server $i (pid $PID) exited during startup; last lines of $DIR/kvserver.log:" >&2
      tail -n 5 "$DIR/kvserver.log" >&2
      exit 1
    fi
    RC=0; "$PY" -m kvstore.probe "$HOST" "$PORT" "$PID" "$i" "$N" || RC=$?
    if [ "$RC" -eq 0 ]; then READY=1; break; fi
    if [ "$RC" -eq 2 ]; then
      kill -TERM "$PID" 2>/dev/null || true
      rm -f "$DIR/kvserver.pid"
      echo "error: server $i: a different process answers on $HOST:$PORT (see above); see $DIR/kvserver.log" >&2
      exit 1
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
