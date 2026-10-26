# Distributed Key-Value Store (v1)

Hash-partitioned Python key-value store: 1–4 servers, each one asyncio process with a SQLite
database (WAL, `synchronous=FULL`, `fullfsync=ON`) and an in-memory copy of committed state.
Writes are group-committed and acknowledged only after the transaction is durable.

Docs: [requirements](docs/requirement.md) · [design](docs/design.md) ·
[spike measurements](spike/README.md)

## Quickstart

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    scripts/start.sh                 # 3 servers from cluster.json on 127.0.0.1:7300-7302
    curl -X PUT --data-binary 'running' http://127.0.0.1:7300/keys/am9iLTE   # key "job-1"
    curl http://127.0.0.1:7300/keys/am9iLTE
    scripts/stop.sh

The key in the URL is base64url. A server answers `421 WRONG_OWNER <id>` if the key hashes
to another server; the client library (`kvstore.client.KVClient`) routes automatically.

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

    .venv/bin/python -m kvstore.bench --config cluster.json --rate 1000 --seconds 60 --out results/n3.csv

Open loop at a fixed arrival rate, 80% Get / 15% Put / 5% Delete, uniform keys over 100K,
1 KiB values, 32 keep-alive connections per server. Latency is measured client-side and
includes queueing. Exit code 0 means read p99 < 50 ms and write p99 < 100 ms.

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
