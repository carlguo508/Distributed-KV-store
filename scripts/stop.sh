#!/usr/bin/env bash
# Stop every server listed in a cluster config. Never touches kv.db, kv.db-wal or kv.db-shm
# (design.md §6): deleting the -wal file could drop committed transactions not yet checkpointed.
# usage: scripts/stop.sh [cluster.json]
set -euo pipefail
CONFIG=${1:-cluster.json}
cd "$(dirname "$0")/.."
PY=${PYTHON:-.venv/bin/python}

for DIR in $("$PY" -c 'import json,sys; print("\n".join(s["data_dir"] for s in json.load(open(sys.argv[1]))["servers"]))' "$CONFIG"); do
  PIDFILE="$DIR/kvserver.pid"
  if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    # SIGTERM, not a graceful drain (design.md §9): in-flight writes were never acked, and
    # acked writes are already on disk, so nothing acknowledged is lost (§7 table).
    if kill -0 "$PID" 2>/dev/null; then
      kill -TERM "$PID" && echo "stopped pid $PID ($DIR)"
    else
      echo "pid $PID ($DIR) not running"
    fi
    rm -f "$PIDFILE"
  fi
done
