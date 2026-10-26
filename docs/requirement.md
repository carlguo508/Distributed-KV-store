# Requirements: Distributed Key-Value Store (v1)

Source: `takehome_assignment_prompt.md`. This document restates the requirements, records the
v1 scope decisions, lists the remaining ambiguities with the chosen resolution, and defers
design topics to the design doc.

## 0. Scope decisions for v1

| Decision | Choice |
|----------|--------|
| Target | All mandatory requirements: Put/Get/Delete, per-key linearizability, restart recovery, latency goals at 1K ops/s |
| Optional requirements (durability across server loss, availability on failure, partition handling) | Not in v1. Design must not preclude adding replication later |
| Language | Python. Spike confirmed p99 targets at 1K ops/s with fsync'd writes ([spike/README.md](../spike/README.md)) |
| Replication / consensus | None in v1. Raft or alternatives deferred |
| Dataset size | Assumed to fit in RAM |
| Time budget | One day |
| Demo | Steady-state load at 1K ops/s with a p99 report, plus a restart test showing data survives |

## 1. Functional requirements

| ID | Requirement | Source |
|----|-------------|--------|
| F1 | `Put(key, value)`: create or fully replace; ack only after the write is durable on disk | prompt |
| F2 | `Get(key)`: return the value, or an explicit `NotFound` | prompt |
| F3 | `Delete(key)`: remove the key; deleting an absent key succeeds | prompt |
| F4 | Keys and values are opaque bytes; an empty value is valid and distinct from a missing key | prompt |
| F5 | Limits: key ≤ 256 B, value ≤ 64 KiB; oversize requests are rejected with an explicit error | derived |
| F6 | Each operation touches exactly one key; no multi-key or transactional operations | prompt |
| F7 | Multiple clients may operate on the same key concurrently | prompt |
| F8 | Request/response format is ours to define | prompt |
| F9 | Static deployment of 1–4 storage servers, each a separate process, deployable on one machine | prompt |
| F10 | Start/stop scripts | deliverables |
| F11 | Workload generator that issues requests and reports latency percentiles at 1K ops/s | deliverables |
| F12 | Coding-agent prompts used to build the system are kept and delivered | deliverables |

Out of scope: authentication, multi-region, dynamic membership, range/scan, TTL,
CAS/conditional writes, transactions, replication (v1).

## 2. Non-functional requirements

| Category | Requirement | v1 |
|----------|-------------|----|
| Consistency | Per-key linearizability: a completed Put/Delete is visible to every later Get; concurrent ops on a key serialize in some single order; no cross-key ordering | must |
| Restart recovery | A process restart (clean or crash) must not lose acknowledged writes; data persisted to disk | must |
| Durability on ack | Every Put/Delete is committed to disk with a full fsync (`fullfsync=ON` on macOS) before the client receives success. No weaker mode | must |
| Visibility | A Get never returns a value that is not yet durable (apply-after-commit) | must |
| Failure policy | If a commit fails, the batch is rejected with an error, nothing is applied, and the server exits | must |
| Performance | 1,000 ops/s sustained, 80% reads / 20% writes+deletes; p99 read < 50 ms, p99 write < 100 ms, healthy system | must |
| Workload | key ~32 B avg (max 256 B), value ~1 KiB avg (max 64 KiB) | sizing input |
| Capacity | Dataset fits in RAM (assumption); persistence still required | constraint |
| Deployment | single region, single machine for the demo, 1–4 processes, static config | constraint |
| Durability across server/disk loss | Acked writes survive loss of one server | deferred |
| Availability after a server failure | Brief interruption only; survivors serve affected data | deferred |
| Partition behavior | Never return stale data as success; return unavailable or time out | deferred |

## 3. Ambiguities relevant to v1

### A1. What "1–4 servers" means without replication
Why it matters: F9 requires multiple processes, but v1 has no replication, so each server must own a disjoint subset of keys.

| Option | Pros | Cons |
|--------|------|------|
| Single server only in v1 | simplest | does not exercise F9; harder to grow later |
| N servers, keys hash-partitioned, each key lives on exactly one server | satisfies F9; same code path for N=1 | losing a server loses its keys (acceptable, durability across server loss is deferred) |

Decision: **hash-partitioned N servers, each key owned by exactly one server**; N=1 is the degenerate case. Who routes a key to its server is a design-phase topic.

### A2. Durability on disk for a single node
Why it matters: F1 says "report success after the write satisfies the durability requirement"; with no replication, that means on disk before ack.

| Option | Safety | Write latency |
|--------|--------|---------------|
| fsync per write | survives power loss | one fsync per write (~1–10 ms on SSD) |
| group commit: batch writes, one fsync per batch | survives power loss | amortized; adds a few ms of queueing |
| write without fsync | survives process crash only, not power loss | lowest |

Decision: **always fsync before ack. No fallback to unsynced writes.** Group commit via one SQLite transaction per batch, `synchronous=FULL`, `fullfsync=ON`, `checkpoint_fullfsync=ON`. Spike measured per-write fsync at put p99 56 ms and group commit at 11 ms, so group commit is the design.

### A3. Dataset size and key cardinality
Resolved by assumption: fits in RAM. Estimate: 200 writes/s × 1 KiB is ~17 GB/day only if every write is a new key; the realistic key set (job status, config) is bounded. Assume ≤ 1M live keys.

### A4. Latency measurement point
Why it matters: "service boundary" could mean the server handler or client-observed.
Decision: latency is measured client-side by the workload generator on the same machine. No server-side latency logging in v1.

### A5. Workload generator

| Choice | Options | Decision |
|--------|---------|----------|
| Load model | closed-loop (fixed concurrency) vs open-loop (fixed arrival rate) | open-loop at 1,000 ops/s |
| Key distribution | uniform vs Zipfian | uniform over ~100K keys |
| Operation mix | fixed 80/20 | 80% Get, 15% Put, 5% Delete |
| Reporting | mean vs histogram | p50/p95/p99/p999 via histogram, printed and saved |

### A6. Retries and idempotency
A timed-out Put may already have been applied. Put/Delete are idempotent for the same value, so no request IDs; a caller may retry after a timeout. The v1 client library does not retry on its own: it raises the timeout to the caller.

### A7. Concurrency semantics
No CAS or versions. Last writer wins by the server's single per-key order.

### A8. Restart test definition
Why it matters: "recover after a process restart" needs a reproducible check.
Decision: write a known set of keys, kill the server with SIGKILL mid-load, restart, verify every acked write is readable and every acked delete returns `NotFound`.

## 4. Design topics

Resolved in [design.md](design.md):

| Topic | Decision |
|-------|----------|
| Wire protocol | HTTP/1.1 (aiohttp); key base64url in path, raw body value; 200/404/400/413/421/500 |
| Client routing | client-side `md5(key) % N` over a static list; server answers 421 `WRONG_OWNER` |
| Storage | SQLite WAL, `synchronous=FULL`, `fullfsync=ON`, `checkpoint_fullfsync=ON`; SQLite handles crash recovery and page reuse |
| Runtime model | one asyncio process per server; SQLite on a single writer thread; batched transactions |
| Visibility | in-memory dict loaded from committed state at startup, updated only after COMMIT |
| Failure policy | commit failure: 500 for the batch, process exits |

Deferred beyond v1: replication and consensus (Raft or alternative), linearizable replicated reads, failure detection, replacing a lost server, write-queue backpressure, health endpoint, consistency checker in the workload generator, graceful drain on SIGTERM.

## 5. Assumptions, no answer needed

- Static config file lists server IDs, addresses, ports; all on localhost with distinct ports and data dirs.
- No TLS, no auth, no multi-tenancy.
- macOS and Linux supported; single-machine demo only.
- Oversize key/value returns an invalid-argument error; empty key is rejected.
- Generator and servers share one laptop; absolute latencies are not representative of a real network, but the p99 goals are enforced as written.
