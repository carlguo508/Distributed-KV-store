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

## Carry into the design

- Append-only log, `F_FULLFSYNC` on macOS and `os.fsync` on Linux, group commit with one
  flusher task, fsync in an executor.
- Ack only after the batch containing the write has been flushed.
- asyncio, one process per server, hash partitioning across servers.
