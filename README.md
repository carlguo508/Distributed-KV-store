# Distributed Key-Value Store (v1)

Hash-partitioned Python key-value store: 1–4 servers, each one asyncio process with a SQLite
database (WAL, `synchronous=FULL`, `fullfsync=ON`) and an in-memory copy of committed state.
Writes are group-committed and acknowledged only after the transaction is durable.

Docs: [requirements](docs/requirement.md) · [design](docs/design.md) ·
[early trade-off notes](docs/tradeoff.md) · [spike measurements](spike/README.md) ·
[checkpoint spike](spike/checkpoint/README.md)

## Quickstart

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/pytest -q              # full test suite, including integration tests; all should pass
    scripts/start.sh                 # 3 servers from cluster.json on 127.0.0.1:7300-7302

The key in the URL is base64url: key `job-1` is `am9iLTE`. Each key is owned by exactly one
server, `md5(key) % N`. Under the 3-server `cluster.json`, `job-1` belongs to **server 2 at
127.0.0.1:7302**. Any other server refuses it with `421 WRONG_OWNER <owner id>`:

    curl -s -w ' %{http_code}\n' http://127.0.0.1:7300/keys/am9iLTE
    # WRONG_OWNER 2 421          <- server 0 is not the owner, and names the owner

    curl -s -w ' %{http_code}\n' -X PUT --data-binary 'running' http://127.0.0.1:7302/keys/am9iLTE
    #  200                       <- owner accepts; acked after the commit is fsynced
    curl -s -w ' %{http_code}\n' http://127.0.0.1:7302/keys/am9iLTE
    # running 200

    scripts/stop.sh

The client library (`kvstore.client.KVClient`) computes the owner itself, so it gets a 421
only if its cluster file differs from the servers'. To find the encoding and owner of your own key:

    .venv/bin/python -c "from kvstore.config import owner_of, load_config; from kvstore.keycodec import encode_key; k=b'my-key'; print(encode_key(k), owner_of(k, load_config('cluster.json').n))"

`scripts/start.sh` only reports a server as up when that server is the process the script just
launched. For each server it:

- refuses a port that is already in use, and prints the pid and command line holding it plus
  how to fix it (`scripts/stop.sh <its config>`, or `kill <pid>` if its pid file is gone);
- waits up to `START_TIMEOUT` seconds (default 20) until `GET /info` on the port returns the
  expected pid, server id and N. It fails at once, showing the tail of the server's log, if the
  server exits during startup, and fails if a different process answers.

`cluster.json` (N=3, ports 7300-7302) and `cluster-1.json` (N=1, port 7400) share no port or
data directory, so a server left over from one cannot answer for the other.
`scripts/stop.sh` never deletes `kv.db`, `kv.db-wal` or `kv.db-shm`.

## API

| Op | Request | Success | Errors |
|----|---------|---------|--------|
| Put | `PUT /keys/{b64url}` body = value | 200 | 400, 413, 421, 500 |
| Get | `GET /keys/{b64url}` | 200 + body (empty allowed) | 404 `NotFound`, 400, 421 |
| Delete | `DELETE /keys/{b64url}` | 200 (also if absent) | 400, 421, 500 |
| Info | `GET /info` | 200 + JSON `{"pid", "id", "n"}` | – |

## Tests

    .venv/bin/pytest -q                       # unit + integration (starts real processes)
    .venv/bin/pytest -q -m "not integration"  # unit only

## Workload generator

`kvbench` first **prefills** the store: it writes every key in the key space once, so timed
Gets read real values instead of mostly missing. Prefill is not measured; 100K keys took about
30 s on one server. The timed run is then an open loop at a fixed arrival rate with 80% Get /
15% Put / 5% Delete over uniformly chosen keys. Latency is measured client-side and includes
queueing. It prints a p50/p95/p99/p99.9/max table per operation, plus the share of Gets that
found their key versus NotFound; the CSV carries the same split in its `hits` and `not_found`
columns. Exit code 0 means read p99 < 50 ms, write p99 < 100 ms and no errors, including
prefill errors; otherwise 1. The servers in the config must already be running.

| Flag | Meaning | Default |
|------|---------|---------|
| `--config` | Cluster file listing the servers to load; sets N and routing | `cluster.json` |
| `--rate` | Target requests per second, issued on a fixed schedule (open loop) | `1000` |
| `--seconds` | How long to issue requests | `60` |
| `--keys` | Size of the key space; keys are picked uniformly from it | `100000` |
| `--value-size` | Bytes per Put value | `1024` |
| `--conns` | Keep-alive connections (and concurrent workers) per server | `32` |
| `--seed` | Random seed for the op mix and key choice; omit for a different run each time | none |
| `--out` | Write the per-operation results as CSV to this path; omit to print only | none |
| `--no-prefill` | Skip the prefill, e.g. when the same key space is already loaded | prefill on |
| `--per-second` | Print write p50/p99/max, errors and Get p99 for each second of the timed run; with `--out`, also save `<out>.per-second.csv` | off |

Examples:

    # Short smoke run against one server, no CSV
    scripts/start.sh cluster-1.json
    .venv/bin/python -m kvstore.bench --config cluster-1.json --rate 200 --seconds 5
    scripts/stop.sh cluster-1.json

    # Same load on N=1 and N=3, each saved to its own CSV
    rm -rf data && scripts/start.sh cluster-1.json
    .venv/bin/python -m kvstore.bench --config cluster-1.json --seconds 60 --seed 1 --out results/exp-n1.csv
    scripts/stop.sh cluster-1.json
    rm -rf data && scripts/start.sh cluster.json
    .venv/bin/python -m kvstore.bench --config cluster.json --seconds 60 --seed 1 --out results/exp-n3.csv
    scripts/stop.sh cluster.json

    # Heavier experiment: 2x the target rate with 4 KiB values
    scripts/start.sh
    .venv/bin/python -m kvstore.bench --rate 2000 --value-size 4096 --seconds 30 --out results/exp-2k-4k.csv
    scripts/stop.sh

`rm -rf data` deletes every stored key; it gives each run a fresh, equal starting point.
Before a measured run, `pgrep -fl kvstore.server` should print nothing: an idle leftover
server is harmless to correctness but shares the CPU and disk with the run.

**Write experiments to new CSV files.** `results/n1.csv` and `results/n3.csv` are the committed
measurements behind the Results table below; overwriting them changes the record.

## Results

Machine: MacBook Pro (Mac14,9), Apple M2 Pro, 16 GB RAM, macOS 26.5.1, Python 3.14.6.
Date: 2026-10-26. 60 s at 1,000 ops/s, `--seed 1`, fresh data directory for each run, 100K
keys prefilled (about 30 s, not measured), so 98.6% of Gets found a value. Generator and
servers on the same machine, with no other kvserver running. Latency is measured from each
request's scheduled time. All latencies in ms.

| Servers | Get p99 | Put p99 | Delete p99 | Get p99.9 | Put p99.9 | Put max | Errors | Result |
|---------|---------|---------|------------|-----------|-----------|---------|--------|--------|
| 1 | 0.95 | 14.48 | 14.04 | 2.68 | 29.86 | 67.40 | 0 | PASS |
| 3 | 0.73 | 30.90 | 31.73 | 1.53 | 35.95 | 41.15 | 0 | PASS |

Writes are slower on 3 servers than on 1 (Put p50 11.8 vs 7.5 ms): the three processes share
one disk, and each commit's full fsync waits for the drive cache flush, so they likely queue
behind each other. Reads never touch SQLite and are not affected.

Raw CSVs: [results/n1.csv](results/n1.csv), [results/n3.csv](results/n3.csv); write latency per
second of each run: [results/n1.per-second.csv](results/n1.per-second.csv),
[results/n3.per-second.csv](results/n3.per-second.csv).

Restart test: `tests/test_restart.py` SIGKILLs a server under load, restarts it and verifies
every acknowledged write. Result: PASS, last run 2026-10-26 on the current code (background
checkpoints included). A manual demo on the 3-server cluster on 2026-10-25 (SIGKILL server 1,
restart it with `scripts/start.sh`) also passed: the restarted server loaded
its committed keys and the following 5 s run at 1,000 ops/s passed with 0 errors.

SIGKILL ends the process but not the OS, so the restart test proves that acknowledged writes
reached SQLite before the ack; it cannot prove the fsync. Power-loss durability rests on the
startup check that `synchronous=FULL` and `fullfsync=ON` are in effect.

## Layout

    kvstore/config.py    cluster.json loading, owner_of = md5(key) % N
    kvstore/keycodec.py  base64url key codec, size limits
    kvstore/storage.py   SQLite + writer thread, batched commits, apply-after-commit, fail-stop,
                         background PASSIVE checkpoints, WAL bounded at 64 MB
    kvstore/server.py    aiohttp handlers and startup order
    kvstore/client.py    async client with routing
    kvstore/bench.py     kvbench workload generator
    kvstore/probe.py     start.sh readiness probe: GET /info must match the launched pid, id and N
    scripts/             start.sh / stop.sh
    tests/               pytest suite: unit, integration, restart (SIGKILL) test
    docs/                requirements, design, trade-off notes, implementation plan, slides
    results/             committed benchmark CSVs behind the Results table
    prompts/v1/          coding-agent prompts used to build v1 (design, code, review)
    spike/               feasibility measurements that drove the design
