#!/usr/bin/env bash
# usage: ./run.sh <perwrite|group> <num_servers> <ops_per_sec> <seconds>
set -u
MODE=${1:-group}; N=${2:-1}; RATE=${3:-1000}; SECS=${4:-20}
cd "$(dirname "$0")"; mkdir -p data; PIDS=(); PORTS=()
for i in $(seq 1 "$N"); do
  port=$((7200 + i)); rm -f "data/kv_$i.log"
  python3 proto_server.py "$MODE" "$port" "data/kv_$i.log" & PIDS+=($!); PORTS+=("$port")
done
sleep 0.5
echo "== mode=$MODE servers=$N rate=$RATE ops/s duration=${SECS}s =="
python3 proto_client.py "$(IFS=,; echo "${PORTS[*]}")" "$RATE" "$SECS"
for p in "${PIDS[@]}"; do ps -o %cpu= -p "$p" | awk -v p="$p" '{print "server pid " p " cpu%:", $1}'; kill "$p"; done
wait 2>/dev/null; rm -rf data
