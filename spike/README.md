# Spike: can Python meet the latency targets with fsync'd writes?

Question from [docs/requirement.md](../docs/requirement.md) section 0: can a Python server
sustain 1,000 ops/s (80% Get, 15% Put, 5% Delete) with every write fsync'd before ack, while
keeping p99 below 50 ms for reads and 100 ms for writes?

Answer: **yes, with group commit.** Measured 2026-10-25 on a MacBook (APFS, internal SSD),
Python 3.14, no third-party packages.

## Files

| File | Purpose |
|------|---------|
| `fsync_bench.py` | Raw cost of `os.fsync`, `F_FULLFSYNC`, and plain write for a 1.1 KB append |
| `proto_server.py` | Throwaway asyncio TCP server: dict state, append-only log, fsync per write or group commit |
| `proto_client.py` | Open-loop workload generator; hashes each key to one server; reports p50/p99/max |
| `run.sh` | Starts N servers, runs the client, prints server CPU, stops everything |
| `sqlite_bench.py` | Commit cost of SQLite WAL + `synchronous=FULL` with `fullfsync` OFF vs ON, batched vs single-row |
| `http_server.py` | Second prototype: aiohttp + SQLite, batched transactions on a writer thread, apply-after-commit |
| `http_client.py` | Open-loop HTTP workload generator, routes keys by md5 to the owning server |
| `run_http.sh` | Same as `run.sh` for the HTTP + SQLite prototype (needs the venv below) |

The server and client are not v1 code. They exist only to measure.

## Setup

Requirements: Python 3.10+, macOS or Linux, nothing to install.

```bash
cd spike
python3 fsync_bench.py ./bench.log
./run.sh group 1 1000 20        # mode, number of servers, ops/s, seconds
./run.sh perwrite 1 1000 20
./run.sh group 3 1000 30
```

Modes: `perwrite` issues one `F_FULLFSYNC` per Put/Delete. `group` appends to a buffer and a
single flusher task issues one `F_FULLFSYNC` per batch, then wakes all waiting requests. The
fsync runs in a thread executor so the event loop keeps serving reads.

Client: 16 connections per server, open-loop arrival at the requested rate, so reported latency
includes queueing time. Key distribution is Pareto-skewed over 100K keys, values 1 KiB.

## Results

Raw fsync cost, 300 sequential 1.1 KB appends:

| Call | p50 | p99 | max |
|------|-----|-----|-----|
| write + `os.fsync` | 0.02 ms | 0.15 ms | 0.53 ms |
| write + `F_FULLFSYNC` | 4.05 ms | 7.34 ms | 9.91 ms |
| write only | 0.00 ms | 0.03 ms | 0.09 ms |

End-to-end, targets are read p99 < 50 ms and write p99 < 100 ms:

| Servers | Mode | Load | Get p99 | Put p99 | Del p99 | CPU per server |
|---------|------|------|---------|---------|---------|----------------|
| 1 | per-write fsync | 1K ops/s, 20 s | 36.0 ms | 56.0 ms | 52.2 ms | |
| 1 | group commit | 1K ops/s, 20 s | 0.8 ms | 29.6 ms | 29.9 ms | |
| 1 | group commit | 1K ops/s, 60 s | 0.9 ms | 10.8 ms | 10.8 ms | 14% |
| 1 | group commit | 2K ops/s, 20 s | 0.5 ms | 11.2 ms | 10.5 ms | 13% |
| 3 | group commit | 1K ops/s total, 30 s | 1.7 ms | 15.4 ms | 14.7 ms | 4–9% |
| 3 | per-write fsync | 1K ops/s total, 30 s | 15.7 ms | 34.6 ms | 35.2 ms | 1–8% |

Every row passes. The three-server rows have all three processes on one physical disk, each
issuing its own `F_FULLFSYNC`.

## Findings

1. **`os.fsync` is not durable on macOS.** It returns in 0.02 ms because APFS only pushes data
   to the drive cache. Real durability needs `fcntl(fd, F_FULLFSYNC)` at about 4 ms median.
   On Linux `os.fsync` is the real flush. The storage layer needs a platform switch.
2. **Group commit is required.** At 200 writes/s, serialized per-write `F_FULLFSYNC` spends
   roughly 0.8 s of disk time per second and pushes write p99 past 50 ms. One flush per batch
   brings write p99 to 11–15 ms.
3. **Flush off the event loop.** Running the fsync in a thread executor keeps read p99 under
   2 ms during flushes. Blocking the loop would drag reads up to the flush cost.
4. **One asyncio process per server is enough.** CPU is around 14% at 1K ops/s and 2K ops/s
   still passes, so no multiprocessing is needed in v1.
5. **Three servers sharing one disk is fine at this load.** Contention from three independent
   flushers raised write p99 from 11 ms to 15 ms, far inside the target. Per-write fsync with
   three servers was actually better than with one, because each server flushes a third of the
   writes.
6. **Head-of-line blocking is a client concern.** In per-write mode, reads queued behind a Put
   on the same connection inherited its latency (36 ms read p99 with one server). The v1
   workload generator should use enough connections, or separate read and write connections.
7. **Restart recovery is untested here.** The spike never replays the log. v1 must add replay
   and a crash test as defined in requirement A8.

## Part 2: aiohttp + SQLite prototype

Design decisions taken after part 1: HTTP/1.1 with the key as base64url in the path and the
value as the raw body, SQLite in WAL mode with `synchronous=FULL` and `fullfsync=ON`, a single
asyncio process with SQLite on one writer thread, batched transactions, and apply-after-commit
(the dict is updated only after `COMMIT` returns; a failed commit applies nothing and returns
500). Keys are routed client-side by `md5(key) % N`; a server that receives a key it does not
own answers 421 `WRONG_OWNER`.

### Setup and run

```bash
cd spike
python3 -m venv .venv && .venv/bin/pip install aiohttp
python3 sqlite_bench.py                 # fullfsync overhead, no venv needed
./run_http.sh 1 1000 30                 # servers, ops/s, seconds
./run_http.sh 3 1000 30
```

Manual check with curl (key `job-1`, base64url `am9iLTE`):

```bash
.venv/bin/python http_server.py 7399 smoke.db 1 0 &
curl -X PUT --data-binary 'running' http://127.0.0.1:7399/keys/am9iLTE   # 200
curl http://127.0.0.1:7399/keys/am9iLTE                                  # running, 200
curl -X PUT --data-binary ''        http://127.0.0.1:7399/keys/am9iLTE   # 200, empty value
curl -i http://127.0.0.1:7399/keys/am9iLTE                               # 200, 0-byte body
curl -X DELETE http://127.0.0.1:7399/keys/am9iLTE                        # 200
curl -i http://127.0.0.1:7399/keys/am9iLTE                               # 404 NotFound
```

### Results

SQLite 3.53.2, WAL, `synchronous=FULL`, 600 one-KiB rows, commit latency:

| fullfsync | rows per commit | commit p50 | commit p99 | cost per row |
|-----------|-----------------|------------|------------|--------------|
| OFF | 1 | 0.06 ms | 1.58 ms | 0.12 ms |
| OFF | 5 | 0.14 ms | 5.97 ms | 0.11 ms |
| OFF | 20 | 0.43 ms | 7.95 ms | 0.07 ms |
| ON | 1 | 4.13 ms | 8.85 ms | 4.61 ms |
| ON | 5 | 4.92 ms | 12.18 ms | 0.98 ms |
| ON | 20 | 5.89 ms | 18.20 ms | 0.32 ms |

End-to-end, aiohttp + SQLite, 32 client connections per server, open-loop, 30 s:

| Servers | Load | Get p99 | Put p99 | Del p99 | Unexpected statuses | CPU per server |
|---------|------|---------|---------|---------|---------------------|----------------|
| 1 | 1K ops/s | 0.85 ms | 11.3 ms | 11.0 ms | 0 | 20% |
| 3 | 1K ops/s total | 0.81 ms | 18.2 ms | 17.3 ms | 0 | 1–12% |

Both pass the 50 ms read and 100 ms write targets.

### Findings

1. **`fullfsync=ON` is the whole cost of durability, and it is affordable.** With it OFF a
   commit is sub-millisecond because macOS only reaches the drive cache. With it ON every
   commit pays one `F_FULLFSYNC`, about 4 ms, the same as the raw call in part 1. Batching
   amortizes it: 20 rows per commit brings the per-row cost from 4.6 ms to 0.3 ms.
2. **The HTTP stack adds little.** aiohttp + SQLite at N=1 gives put p99 of 11 ms, the same as
   the raw TCP + append-log prototype. Reads stay under 1 ms. CPU rose from 14% to 20%.
3. **Three servers on one disk still pass.** Write p99 went from 11 ms to 18 ms, in line with
   part 1. The uneven CPU across the three servers comes from the skewed key distribution:
   hot keys land on whichever server owns them.
4. **Apply-after-commit costs nothing measurable.** The writer thread applies to the dict after
   `COMMIT` and wakes waiters through `call_soon_threadsafe`; no latency difference versus
   apply-early in part 1.
5. **Not covered here:** restart replay is only the trivial `SELECT k, v FROM kv` at startup;
   the SIGKILL-mid-load test from requirement A8 still needs to be run. WAL checkpointing
   happens automatically (default every 1000 pages) and with `checkpoint_fullfsync=ON` it also
   pays a full fsync; its effect on tail latency was not isolated in this run.

## Carry into the design

- aiohttp HTTP/1.1 API; key base64url in the path, raw value body; 404 for missing, 200 with
  empty body for an empty value; 421 `WRONG_OWNER` for a misrouted key.
- SQLite, WAL, `synchronous=FULL`, `fullfsync=ON`, `checkpoint_fullfsync=ON`; one writer
  thread, one transaction per batch, apply to the in-memory dict only after `COMMIT`.
- Ack only after the transaction containing the write has committed; a failed commit applies
  nothing and returns an error.
- Single asyncio process per server, client-side md5 hash partitioning across servers.
