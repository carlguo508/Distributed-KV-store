# Distributed Key-Value Store (v1)

Hash-partitioned Python key-value store: 1–4 servers, each one asyncio process with a SQLite
database (WAL, `synchronous=FULL`, `fullfsync=ON`) and an in-memory copy of committed state.
Writes are group-committed and acknowledged only after the transaction is durable.

Docs: [requirements](docs/requirement.md) · [design](docs/design.md) ·
[spike measurements](spike/README.md)

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

`scripts/start.sh` waits up to `START_TIMEOUT` seconds (default 20) for each server's port and
exits non-zero, naming the server's log file, if one does not come up. `scripts/stop.sh` never
deletes `kv.db`, `kv.db-wal` or `kv.db-shm`.

## API

| Op | Request | Success | Errors |
|----|---------|---------|--------|
| Put | `PUT /keys/{b64url}` body = value | 200 | 400, 413, 421, 500 |
| Get | `GET /keys/{b64url}` | 200 + body (empty allowed) | 404 `NotFound`, 400, 421 |
| Delete | `DELETE /keys/{b64url}` | 200 (also if absent) | 400, 421, 500 |

## Tests

    .venv/bin/pytest -q                       # unit + integration (starts real processes)
    .venv/bin/pytest -q -m "not integration"  # unit only

## Workload generator

`kvbench` runs an open loop at a fixed arrival rate with 80% Get / 15% Put / 5% Delete over
uniformly chosen keys. Latency is measured client-side and includes queueing. It prints a
p50/p95/p99/p99.9/max table per operation. Exit code 0 means read p99 < 50 ms, write p99 <
100 ms and no errors; otherwise 1. The servers in the config must already be running.

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

**Write experiments to new CSV files.** `results/n1.csv` and `results/n3.csv` are the committed
measurements behind the Results table below; overwriting them changes the record.

## Results

Machine: MacBook Pro (Mac14,9), Apple M2 Pro, 16 GB RAM, macOS 26.5.1, Python 3.14.6.
Date: 2026-10-25. 60 s at 1,000 ops/s, fresh data directory for each run. Generator and
servers on the same machine. All latencies in ms.

| Servers | Get p99 | Put p99 | Delete p99 | Get p99.9 | Put p99.9 | Errors | Result |
|---------|---------|---------|------------|-----------|-----------|--------|--------|
| 1 | 1.14 | 19.69 | 20.92 | 13.62 | 38.17 | 0 | PASS |
| 3 | 0.92 | 22.15 | 18.72 | 4.16 | 38.49 | 0 | PASS |

Raw CSVs: [results/n1.csv](results/n1.csv), [results/n3.csv](results/n3.csv).

Restart test: `tests/test_restart.py` SIGKILLs a server under load, restarts it and verifies
every acknowledged write. Result: PASS, 2026-10-25. A manual demo on the 3-server cluster
(SIGKILL server 1, restart it with `scripts/start.sh`) also passed: the restarted server loaded
its committed keys and the following 5 s run at 1,000 ops/s passed with 0 errors.

SIGKILL ends the process but not the OS, so the restart test proves that acknowledged writes
reached SQLite before the ack; it cannot prove the fsync. Power-loss durability rests on the
startup check that `synchronous=FULL` and `fullfsync=ON` are in effect.

## Layout

    kvstore/config.py    cluster.json loading, owner_of = md5(key) % N
    kvstore/keycodec.py  base64url key codec, size limits
    kvstore/storage.py   SQLite + writer thread, batched commits, apply-after-commit, fail-stop
    kvstore/server.py    aiohttp handlers and startup order
    kvstore/client.py    async client with routing
    kvstore/bench.py     kvbench workload generator
    scripts/             start.sh / stop.sh
    spike/               feasibility measurements that drove the design
