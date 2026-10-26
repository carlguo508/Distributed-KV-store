#!/usr/bin/env bash
# Run T1, T3 and T4 three times each (interleaved, so slow drift in machine state does not land
# on one test), after checking that the disk is quiet.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
echo "=== quiet check $(date '+%F %T'): load $(uptime | sed 's/.*load averages*: //')"
iostat -d -c 3 -w 1 disk0 | tail -1 | awk '{print "disk MB/s now:", $3}'
for run in 1 2 3; do
  for test in T1 T3 T4; do
    "$HERE/run_test.sh" "$test" "$run"
    sleep 5
  done
done
