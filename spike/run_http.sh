#!/usr/bin/env bash
# usage: ./run_http.sh <num_servers> <ops_per_sec> <seconds>
# Needs: python3 -m venv .venv && .venv/bin/pip install aiohttp
set -u
N=${1:-1}; RATE=${2:-1000}; SECS=${3:-20}
cd "$(dirname "$0")"; PY=.venv/bin/python; mkdir -p data; rm -f data/http_*.db*; PIDS=(); PORTS=()
for i in $(seq 0 $((N-1))); do
  port=$((7300 + i))
  $PY http_server.py "$port" "data/http_$i.db" "$N" "$i" & PIDS+=($!); PORTS+=("$port")
done
sleep 1
echo "== aiohttp+sqlite servers=$N rate=$RATE ops/s duration=${SECS}s =="
$PY http_client.py "$(IFS=,; echo "${PORTS[*]}")" "$RATE" "$SECS"
for p in "${PIDS[@]}"; do ps -o %cpu= -p "$p" | awk -v p="$p" '{print "server pid " p " cpu%:", $1}'; done
kill "${PIDS[@]}" 2>/dev/null; wait 2>/dev/null; rm -rf data
