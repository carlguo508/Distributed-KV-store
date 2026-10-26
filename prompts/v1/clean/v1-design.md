# v1-design

## 👤 Prompt

I am working on an assignment to implement a distributed kv store.  Read the
@takehome_assignment_prompt.md  carefully

Create a doc/requirements.md including
1. Functional Requirement of the system
2. Non-functional requirement, such as consistency, durability, recovery,
performance
3. The ambiguities and unspecified items. For each item, list why it matters
and what are the options, what is your perferred choice
4. Questions I need to answer, for now only the ones related to the design,
for other that are not that important, give your assumptions

Be concise, use tables for the tradeoff comparison

## 🤖 Claude

I'll read the prompt (done above), then draft the plan for
doc/requirements.md. The repo contains only the prompt file, so there is no
code to explore. I'll write the plan with the full draft content of the
document.

The plan is written. It contains the full draft of the requirements document,
so execution is a single step: create doc/requirements.md from it.

Writing doc/requirements.md now.

## 👤 Prompt

List the requirements.md again and do not write it yet, I need to do some
manual edit

## 🤖 Claude

Nothing was written yet. Here is the full draft of doc/requirements.md for
your edits.

# Requirements: Distributed Key-Value Store

Source: `takehome_assignment_prompt.md`. This document restates the
requirements, lists
ambiguities with options and a preferred choice, and separates design
questions from assumptions.

## 1. Functional requirements

| ID | Requirement | Source |
|----|-------------|--------|
| F1 | `Put(key, value)`: create or fully replace; ack only after the
durability requirement is met | prompt |
| F2 | `Get(key)`: return the value, or an explicit `NotFound` | prompt |
| F3 | `Delete(key)`: remove the key; deleting an absent key succeeds | prompt
|
| F4 | Keys and values are opaque bytes; an empty value is valid and distinct
from a missing key | prompt |
| F5 | Limits: key ≤ 256 B, value ≤ 64 KiB; oversize requests are rejected
with an explicit error | derived |
| F6 | Each operation touches exactly one key; no multi-key or transactional
operations | prompt |
| F7 | Multiple clients may operate on the same key concurrently | prompt |
| F8 | Request/response format is ours to define | prompt |
| F9 | Static cluster of 1–4 storage servers, each a separate process,
deployable on one machine | prompt |
| F10 | Start/stop scripts and a workload generator demonstrating 1K ops/s
within the latency goals | deliverables |

Out of scope (explicit or implied): authentication, multi-region, dynamic
membership,
range/scan, TTL, CAS/conditional writes, transactions.

## 2. Non-functional requirements

| Category | Requirement | Tier |
|----------|-------------|------|
| Consistency | Per-key linearizability: a completed Put/Delete is visible to
every later Get; concurrent ops on a key serialize in some single order; no
cross-key ordering | must |
| Restart recovery | A process restart must not lose acknowledged writes; data
persisted to disk (WAL and/or snapshot) | must |
| Durability | Acked writes survive permanent loss of any one server including
its disk, so data is on ≥2 servers before ack | nice-to-have (target) |
| Availability | One server failure causes only a brief interruption;
unaffected data stays accessible; affected data is served by survivors |
nice-to-have (target) |
| Partition behavior | Never return stale data as a success; return
`Unavailable` or time out instead (CP, not AP) | nice-to-have (target) |
| Performance | 1,000 ops/s sustained, 80% reads / 20% writes+deletes; p99
read < 50 ms, p99 write < 100 ms at the service boundary on a healthy system |
must |
| Workload | key ~32 B avg (max 256 B), value ~1 KiB avg (max 64 KiB) | sizing
input |
| Deployment | single region, single machine for the demo, 1–4 processes,
static config | constraint |
| Operability | start/stop scripts; enough logging/metrics to demonstrate
failover | derived |

## 3. Ambiguities and unspecified items

Each item: why it matters, the options, and the preferred choice.

### A1. Replication / consensus mechanism
Why it matters: determines consistency, durability and failover; the core
design decision.

| Option | Consistency | Durable on 1 loss | Failover | Complexity |
|--------|-------------|-------------------|----------|------------|
| Raft, one group over all nodes | linearizable | yes if N ≥ 2 | automatic
election | medium |
| Primary-backup, sync replication + external coordinator | linearizable | yes
| needs coordinator / failure detector | medium-high; coordinator is a SPOF
unless replicated |
| Quorum R/W (Dynamo-style) | needs per-key versions + read-repair to be
linearizable | yes | none needed | high to make linearizable |
| Single node + fsync | linearizable | no | none | low |

Preferred: **Raft**. One mechanism satisfies all three optional requirements,
and minority
partitions naturally refuse to serve, which matches the "no stale reads" rule.

### A2. Data placement: single group vs sharding
Why it matters: affects "unaffected data remains accessible" and code size.

| Option | Pros | Cons |
|--------|------|------|
| One Raft group, all keys, all N nodes | simplest; 1K ops/s is far below one
group's capacity | leader failover briefly pauses all keys (acceptable per
prompt) |
| Hash-sharded multi-Raft (8–16 groups across nodes) | failover of one group
affects a subset of keys | more code and config; little benefit at this scale
|
| Consistent hashing, per-key primary | fine-grained placement | needs
separate replication and failover logic |

Preferred: **single Raft group**; keep a key→shard hash in the API layer so
sharding is a later extension.

### A3. Cluster size semantics (N = 1..4)
Why it matters: quorum math changes what durability and availability mean.

| N | Quorum | Failures tolerated for availability | Durable on 1 disk loss |
|---|--------|-------------------------------------|------------------------|
| 1 | 1 | 0 | no (impossible) |
| 2 | 2 | 0 (writes block when one node is down) | yes |
| 3 | 2 | 1 | yes |
| 4 | 3 | 1 | yes |

Preferred: all N nodes are voters in one group. N=1 and N=2 are supported but
degraded; the demo runs N=3.

### A4. Read path (linearizable reads)
Why it matters: the biggest latency and correctness lever for the 80% read
workload.

| Option | Linearizable | Read latency | Notes |
|--------|--------------|--------------|-------|
| Leader-only, through the log | yes | high (disk + replication per read) |
wasteful |
| Leader-only with ReadIndex (heartbeat round) | yes | one RTT, no disk |
standard |
| Leader lease reads | yes, assuming bounded clock drift | local | risk under
clock skew |
| Follower reads | no (stale) | local | violates requirement |

Preferred: **ReadIndex on the leader**; lease reads only as a measured
optimization.

### A5. Client routing
Why it matters: who finds the leader, and how much the client must know.

| Option | Client complexity | Extra hop |
|--------|-------------------|-----------|
| Any node accepts and forwards to the leader | none | one intra-cluster hop
on non-leader |
| Redirect response with leader hint; client retries | small | one round trip
on miss |
| Smart client caches leader | moderate | none in steady state |

Preferred: **any node forwards**, plus a leader hint in responses so the
client can cache it.

### A6. Wire protocol

| Option | Binary-safe bytes | Tooling | Perf |
|--------|-------------------|---------|------|
| gRPC / protobuf | yes | codegen, streaming | good |
| HTTP/1.1 + JSON (base64 values) | awkward | curl-friendly | ok |
| Custom TCP framing | yes | hand-rolled | best |

Preferred: **gRPC** for both the client API and Raft RPC (one stack).

### A7. Storage engine

| Option | Restart recovery | Memory bound | Effort |
|--------|------------------|--------------|--------|
| In-memory map + Raft log on disk + periodic snapshots | yes | dataset must
fit RAM | low–medium |
| Embedded KV (Pebble / RocksDB / bbolt / sled) as state machine | yes |
disk-bound | low (library) |
| Custom LSM / B-tree | yes | disk-bound | high |

Preferred: **in-memory map + WAL + snapshots** if the dataset fits memory (see
Q6); otherwise embedded KV.

### A8. fsync policy
Why it matters: defines what "durable" means on a single disk.

| Option | Safety | Write latency |
|--------|--------|---------------|
| fsync per entry | strongest | ~ms per write; fine for 200 w/s |
| group commit (fsync per batch, few-ms window) | strong | amortized |
| no fsync (OS cache) | loses tail on power loss | lowest |

Preferred: **fsync before vote/ack, with group commit**. "Memory alone is not
sufficient" implies per-node power-loss durability.

### A9. Failure detection and "brief interruption"
Why it matters: the prompt gives no bound.
Preferred: Raft heartbeat 100 ms, election timeout 500–1000 ms, target full
failover < 2 s; the demo measures it.

### A10. Replacing a permanently lost server
Why it matters: membership is static but a disk can be lost.
Preferred: operator restarts the node with the same ID and an empty data dir;
it rejoins and catches up via snapshot + log. No membership-change RPC.

### A11. Dataset size / key cardinality
Why it matters: drives the storage engine choice (A7).
Estimate: 200 writes/s × 1 KiB is ~17 GB/day if every write is a new key;
realistic for "job status / config" is a bounded key set. Assume ≤ 1M live
keys (~1–2 GB) fits in RAM per node.

### A12. Latency measurement point
Why it matters: "service boundary" could mean the server handler or the
client-observed latency.
Preferred: report both; the headline number is client-observed from the
workload generator on the same machine, since that is what a grader can
reproduce.

### A13. Workload generator design

| Choice | Options | Preferred |
|--------|---------|-----------|
| Load model | closed-loop (fixed concurrency) vs open-loop (fixed arrival
rate) | open-loop at 1,000 ops/s; shows true tail latency |
| Key distribution | uniform vs Zipfian | Zipfian over ~100K keys, uniform as
an option |
| Verification | none vs per-key last-write check | lightweight per-key
checker: track acked writes, flag stale reads |
| Reporting | mean vs histogram | p50/p95/p99/p999 via HDR histogram,
CSV/markdown output |

### A14. Retries and idempotency
Why it matters: a timed-out Put may already have been applied.
Preferred: Put/Delete are idempotent for the same value; no request IDs; the
client retries after timeout.

### A15. Concurrency semantics
No CAS or versions required. Last writer wins by log order.

## 4. Open questions and assumptions

### Design questions to answer before the design doc

| # | Question | Why it matters | Default if unanswered |
|---|----------|----------------|-----------------------|
| Q1 | Implementation language? (Go / Rust / Java / Python) | Raft and gRPC
library ecosystem; performance headroom | Go |
| Q2 | Raft from scratch or a library (hashicorp/raft, etcd/raft)? | scratch
shows depth but costs time and risk | library for the core; own state machine,
log store, snapshots, client layer |
| Q3 | Treat the optional requirements (durability, availability, partitions)
as must-do? | decides Raft vs single node + WAL | yes, all three |
| Q4 | Single Raft group or sharded multi-group? | scope, and the demo story
for "unaffected data remains accessible" | single group |
| Q5 | gRPC or HTTP for the client API? | tooling vs curl-ability for the
grader | gRPC; thin HTTP gateway only if cheap |
| Q6 | Expected dataset size: fits RAM or must be disk-indexed? | storage
engine (A7) | fits RAM; map + WAL + snapshots |
| Q7 | Time budget for the take-home? | depth of Q2 and Q4 | 1–2 focused days
|
| Q8 | Must the demo show failover and partitions, or only steady-state load?
| drives chaos scripts in the workload generator | both: kill a node mid-load,
show recovery |

### Minor items: assumptions taken, no answer needed
- Static config file lists node IDs, addresses, ports; nodes run on localhost
with distinct ports and data dirs.
- No TLS, no auth, no multi-tenancy.
- macOS and Linux supported; single-machine demo only.
- Snapshot trigger: every N log entries or a size threshold; log truncated
after snapshot.
- Logging to stdout plus a simple status/metrics endpoint per node.
- The workload generator uses the same client library as production clients.
- Oversize key/value returns `InvalidArgument`; empty key is rejected.
- Generator and servers share one laptop, so absolute latencies are not
representative of a real network; the p99 goals are still enforced as written.

Nothing is on disk yet. Tell me when you're done editing and I'll write it to
doc/requirements.md, or paste your edited version and I'll save that.

## 👤 Prompt

For the questions:
 Q1 Language: Python. In the design phase, check whether Python can hit
  the p99 targets at 1K ops/s with fsync'd writes
Q2 no raft for version 1
Q3 Optional requirements: NOT in v1. I only want to achieve all the the
mandatory requirements
  (Put/Get/Delete, per-key linearizability, restart recovery) in v1, meeting
the latency targets at 1K ops/s.
Q4: Leave the replication and raft design and implementation to the future,
for this specific design choice, leave it to the design phrase later
Q5: Defer this to the design phrase
Q6: Suppose the data can fit in RAM
Q7: one day
Q8: dont do failover demo, show the steady state load at 1k ops/s with the
p99 report and restart test

Keep only the requirement related items in this doc. and for design choices,
drop the ones not used for v1, other goes to a short list, we will discuss
the design later

Show me the revised plan

## 🤖 Claude

Here is the revised draft, scoped to v1 (mandatory requirements only, Python,
no replication).

# Requirements: Distributed Key-Value Store (v1)

Source: `takehome_assignment_prompt.md`. This document restates the
requirements, records the
v1 scope decisions, lists the remaining ambiguities with a preferred choice,
and defers
design topics to the design doc.

## 0. Scope decisions for v1

| Decision | Choice |
|----------|--------|
| Target | All mandatory requirements: Put/Get/Delete, per-key
linearizability, restart recovery, latency goals at 1K ops/s |
| Optional requirements (durability across server loss, availability on
failure, partition handling) | Not in v1. Design must not preclude adding
replication later |
| Language | Python. Design phase must check that Python can hit the p99
targets at 1K ops/s with fsync'd writes |
| Replication / consensus | None in v1. Raft or alternatives deferred |
| Dataset size | Assumed to fit in RAM |
| Time budget | One day |
| Demo | Steady-state load at 1K ops/s with a p99 report, plus a restart test
showing data survives |

## 1. Functional requirements

| ID | Requirement | Source |
|----|-------------|--------|
| F1 | `Put(key, value)`: create or fully replace; ack only after the write is
durable on disk | prompt |
| F2 | `Get(key)`: return the value, or an explicit `NotFound` | prompt |
| F3 | `Delete(key)`: remove the key; deleting an absent key succeeds | prompt
|
| F4 | Keys and values are opaque bytes; an empty value is valid and distinct
from a missing key | prompt |
| F5 | Limits: key ≤ 256 B, value ≤ 64 KiB; oversize requests are rejected
with an explicit error | derived |
| F6 | Each operation touches exactly one key; no multi-key or transactional
operations | prompt |
| F7 | Multiple clients may operate on the same key concurrently | prompt |
| F8 | Request/response format is ours to define | prompt |
| F9 | Static deployment of 1–4 storage servers, each a separate process,
deployable on one machine | prompt |
| F10 | Start/stop scripts | deliverables |
| F11 | Workload generator that issues requests and reports latency
percentiles at 1K ops/s | deliverables |
| F12 | Coding-agent prompts used to build the system are kept and delivered |
deliverables |

Out of scope: authentication, multi-region, dynamic membership, range/scan,
TTL,
CAS/conditional writes, transactions, replication (v1).

## 2. Non-functional requirements

| Category | Requirement | v1 |
|----------|-------------|----|
| Consistency | Per-key linearizability: a completed Put/Delete is visible to
every later Get; concurrent ops on a key serialize in some single order; no
cross-key ordering | must |
| Restart recovery | A process restart (clean or crash) must not lose
acknowledged writes; data persisted to disk | must |
| Performance | 1,000 ops/s sustained, 80% reads / 20% writes+deletes; p99
read < 50 ms, p99 write < 100 ms, healthy system | must |
| Workload | key ~32 B avg (max 256 B), value ~1 KiB avg (max 64 KiB) | sizing
input |
| Capacity | Dataset fits in RAM (assumption); persistence still required |
constraint |
| Deployment | single region, single machine for the demo, 1–4 processes,
static config | constraint |
| Durability across server/disk loss | Acked writes survive loss of one server
| deferred |
| Availability after a server failure | Brief interruption only; survivors
serve affected data | deferred |
| Partition behavior | Never return stale data as success; return unavailable
or time out | deferred |

## 3. Ambiguities relevant to v1

### A1. What "1–4 servers" means without replication
Why it matters: F9 requires multiple processes, but v1 has no replication, so
each server must own a disjoint subset of keys.

| Option | Pros | Cons |
|--------|------|------|
| Single server only in v1 | simplest | does not exercise F9; harder to grow
later |
| N servers, keys hash-partitioned, each key lives on exactly one server |
satisfies F9; same code path for N=1 | losing a server loses its keys
(acceptable, durability is defered) |

Preferred: **hash-partitioned N servers**, N=1 is the degenerate case. Who
routes a key to its server is a design-phase topic.

### A2. Durability on disk for a single node
Why it matters: F1 says "report success after the write satisfies the
durability requirement"; with no replication, that means on-disk before ack.

| Option | Safety | Write latency |
|--------|--------|---------------|
| fsync per write | survives power loss | one fsync per write (~1–10 ms on
SSD) |
| group commit: batch writes, one fsync per batch | survives power loss |
amortized; adds a few ms of queueing |
| write without fsync | survives process crash only, not power loss | lowest |

Preferred: **fsync before ack, with group commit** if per-write fsync does not
meet p99 in Python. Process-crash-only durability is the fallback only if
measurements force it, and it must be stated in the design doc.

### A3. Dataset size and key cardinality
Resolved by assumption: fits in RAM. Estimate: 200 writes/s × 1 KiB is ~17
GB/day only if every write is a new key; the realistic key set (job status,
config) is bounded. Assume ≤ 1M live keys.

### A4. Latency measurement point
Why it matters: "service boundary" could mean the server handler or
client-observed.
Preferred: headline number is client-observed from the workload generator on
the same machine; also log server-side handler latency.

### A5. Workload generator

| Choice | Options | Preferred |
|--------|---------|-----------|
| Load model | closed-loop (fixed concurrency) vs open-loop (fixed arrival
rate) | open-loop at 1,000 ops/s |
| Key distribution | uniform vs Zipfian | Zipfian over ~100K keys, uniform as
an option |
| Operation mix | fixed 80/20 | 80% Get, 15% Put, 5% Delete |
| Verification | none vs per-key last-write check | per-key checker: track
acked writes, flag stale reads |
| Reporting | mean vs histogram | p50/p95/p99/p999 via histogram, printed and
saved |

### A6. Retries and idempotency
A timed-out Put may already have been applied. Put/Delete are idempotent for
the same value, so no request IDs; the client retries after timeout.

### A7. Concurrency semantics
No CAS or versions. Last writer wins by the server's single per-key order.

### A8. Restart test definition
Why it matters: "recover after a process restart" needs a reproducible check.
Preferred: write a known set of keys, kill the server with SIGKILL mid-load,
restart, verify every acked write is readable and every acked delete returns
`NotFound`.

## 4. Deferred design topics

To be decided in the design doc; not requirements.

- Wire protocol and request/response format (HTTP/JSON, gRPC, custom framing).
- Client routing to the owning server (client-side hashing vs server
forwarding).
- Storage layout: in-memory dict + append-only log + snapshots vs embedded
store (SQLite, LMDB).
- Python runtime model: asyncio vs threads vs multiprocess; verify fsync'd
writes meet p99 at 1K ops/s.
- Log compaction / snapshot trigger.
- Future: replication and consensus (Raft or alternative), read path for
linearizable replicated reads, failure detection, replacing a lost server.

## 5. Assumptions, no answer needed

- Static config file lists server IDs, addresses, ports; all on localhost with
distinct ports and data dirs.
- No TLS, no auth, no multi-tenancy.
- macOS and Linux supported; single-machine demo only.
- Oversize key/value returns an invalid-argument error; empty key is rejected.
- Generator and servers share one laptop; absolute latencies are not
representative of a real network, but the p99 goals are enforced as written.

Still unwritten. Tell me when to save it to doc/requirements.md, or send your
edits.

The revised draft above is ready for your edits. I also saved your v1 scope
decisions to memory so later sessions do not re-propose Raft or replication
for v1.

Summary of what changed from the first draft:

- New section 0 records the eight answers as v1 scope decisions.
- Optional requirements moved to "deferred" rows in the non-functional table
  instead of targets.
- Dropped the replication, consensus, cluster-size quorum, read-path,
  client-routing, failover-timing and lost-server items.
- Kept only v1-relevant ambiguities: hash partitioning across 1–4 servers,
  fsync policy, dataset size, latency measurement point, workload generator,
  retries, concurrency, and a concrete restart test.
- Section 4 is the short list of deferred design topics for the design
  discussion, including the Python fsync feasibility check.

Nothing is written to doc/requirements.md yet. Send your edits or tell me to
save it as shown.

## 👤 Prompt

A1: Accepted — hash-partitioned N servers, each key owned by exactly
   one server;
A2: we should always use fsync before ack to make sure durability, no
fallback you suggested. Use group commit** if per-write fsync does not meet
p99 in Python
Accept other assumptions. Now approved, write the requirement doc to
docs/requirement.md and commit

## 🤖 Claude

Writing docs/requirement.md with A1 accepted and A2 tightened to mandatory
fsync-before-ack, then committing.

Done. The requirements doc is written and committed.

- File: docs/requirement.md, committed as c87e7be.
- A1 recorded as a decision: hash-partitioned N servers, one owner per key,
  N=1 as the degenerate case.
- A2 tightened: fsync before ack is now a must-level non-functional
  requirement with no unsynced fallback. Per-write fsync first, group commit
  only if the write p99 misses.
- Section 4 holds the deferred design topics (protocol, routing, storage
  layout, Python runtime model, future replication) for the design discussion.

One note: takehome_assignment_prompt.md is still untracked. I left it out
since you only asked to commit the doc. Say so if you want it added.

Next step is the design doc, starting with the Python fsync feasibility check
at 1K ops/s.

## 👤 Prompt

check the feasibility using python to complete this task at 1k ops/s and meet
the p99 requirement

## 🤖 Claude

I'll measure rather than guess: first raw fsync cost on this Mac, then a
throwaway asyncio server with a fsync'd append log driven at 1K ops/s
open-loop, reporting p99.

Key finding: macOS's os.fsync doesn't actually flush to disk (it's
near-instant and misleading), while F_FULLFSYNC is the real deal at ~4ms
median/7ms p99—meaning per-write fsync at 200 writes/s would eat ~0.8s of disk
time per second, so group commit is likely needed on Mac. Moving on to build
the end-to-end prototype. · summary

Both modes meet the targets. Group commit gives large margin on reads. Two
more checks: headroom at 2x load, and a longer 60 s run for stability.

Shell word-splitting bug in my loop (zsh does not split $cfg). Rerunning with
explicit arguments.

Python is feasible. A throwaway asyncio server with a real-fsync append log
meets both p99 targets at 1K ops/s with wide margin, provided writes use group
commit.

Measurements (Python 3.14, this Mac, 16 client connections, open-loop 80/15/5
mix, latency includes queueing):

┌─────────────────────────────────┬────────┬────────┬────────┬───────────┐
│             Config              │  Get   │  Put   │  Del   │  Server   │
│                                 │  p99   │  p99   │  p99   │    CPU    │
├─────────────────────────────────┼────────┼────────┼────────┼───────────┤
│ Per-write F_FULLFSYNC, 1K       │ 36 ms  │ 56 ms  │ 52 ms  │           │
│ ops/s, 20 s                     │        │        │        │           │
├─────────────────────────────────┼────────┼────────┼────────┼───────────┤
│ Group commit, 1K ops/s, 20 s    │ 0.8 ms │ 30 ms  │ 30 ms  │           │
├─────────────────────────────────┼────────┼────────┼────────┼───────────┤
│ Group commit, 1K ops/s, 60 s    │ 0.9 ms │ 10.8   │ 10.8   │ 14%       │
│                                 │        │ ms     │ ms     │           │
├─────────────────────────────────┼────────┼────────┼────────┼───────────┤
│ Group commit, 2K ops/s, 20 s    │ 0.5 ms │ 11.2   │ 10.5   │ 13%       │
│                                 │        │ ms     │ ms     │           │
└─────────────────────────────────┴────────┴────────┴────────┴───────────┘

Targets are 50 ms read, 100 ms write. Both fsync strategies pass, but
per-write fsync leaves little margin and group commit is the clear choice.

Findings that must shape the design:

- macOS fsync is not durable. os.fsync returned in 0.02 ms, which means the
  data only reached the drive cache. Real durability needs fcntl(fd,
  F_FULLFSYNC), which costs about 4 ms median and 7 ms p99 per call. On Linux,
  os.fsync is the real thing. The storage layer needs a platform switch.
- Group commit is required, not optional. At 200 writes/s, serialized
  per-write F_FULLFSYNC consumes roughly 0.8 s of disk time per second. One
  flush per batch brings write p99 to about 11 ms.
- The flush must run off the event loop. The prototype runs the fsync in a
  thread executor so reads keep flowing during a flush. With that, read p99 is
  under 1 ms even at 2K ops/s.
- Single process per server is enough. Server CPU sat around 14% at 1K ops/s,
  so asyncio in one process has headroom. No multiprocessing needed for v1.
- Head-of-line blocking matters for the client. In per-write mode, reads
  queued behind a put on the same connection inherited its latency (36 ms read
  p99). The workload generator should use enough connections, or separate
  read and write connections.

The prototype scripts are in the session scratchpad, not the repo:
/private/tmp/claude-502/-Users-Carl-Guo-Desktop-Distributed-kv-store/54546b18-
1374-46f9-80f9-a61fd8c938c2/scratchpad/proto_server.py, proto_client.py, and
fsync_bench.py. They are throwaway, not v1 code. Nothing in the repo changed.
These results and the F_FULLFSYNC rule should go into the design doc when we
write it.

## 👤 Prompt

Copy the prototype scripts into a spike directory, and write a doc to
indicate how to setup and run the test, what is the result and findings,  Add
one more run: N=3 servers
on this machine — they share one physical disk and each issues its own
F_FULLFSYNC

## 🤖 Claude

I'll copy the scripts into spike/, extend the client to route keys by hash
across multiple servers, run the N=3 test, and write the doc.

N=3 passes comfortably with group commit. Writing the spike doc now.

The spike directory is in place and the N=3 run is done. Not committed, since
you did not ask for that this time.

What's in spike/:
- README.md (spike/README.md) with setup, how to run, the full results table,
  and findings.
- fsync_bench.py (spike/fsync_bench.py), proto_server.py
  (spike/proto_server.py), proto_client.py (spike/proto_client.py) copied from
  the scratchpad. The client now takes a comma-separated port list and routes
  each key to one server by hash.
- run.sh (spike/run.sh) starts N servers, runs the client, prints per-server
  CPU, and tears everything down. Example: ./spike/run.sh group 3 1000 30.

N=3 result, three processes sharing one SSD, each with its own F_FULLFSYNC, 1K
ops/s total for 30 s:

┌─────────────────┬─────────┬─────────┬─────────┬────────────────┐
│      Mode       │ Get p99 │ Put p99 │ Del p99 │ CPU per server │
├─────────────────┼─────────┼─────────┼─────────┼────────────────┤
│ group commit    │ 1.7 ms  │ 15.4 ms │ 14.7 ms │ 4–9%           │
├─────────────────┼─────────┼─────────┼─────────┼────────────────┤
│ per-write fsync │ 15.7 ms │ 34.6 ms │ 35.2 ms │ 1–8%           │
└─────────────────┴─────────┴─────────┴─────────┴────────────────┘

Both pass. Disk contention from three independent flushers raised group-commit
write p99 from 11 ms to 15 ms, still far inside the 100 ms target. Per-write
fsync with three servers beat the single-server case because each server
flushes only a third of the writes.

The README also records the macOS os.fsync finding, the group-commit and
executor requirements, and that log replay and the restart test are still
untested and belong to v1.

## 👤 Prompt

I have verified this works. Now commit this

## 🤖 Claude

Committed as 9dab02d with all five files under spike/.

takehome_assignment_prompt.md remains untracked, as before. Next up is the
design doc, which can now cite the spike results for the storage layer
decisions.

## 👤 Prompt

Based on the spike result, we should use python and use group commit for this
design. But I think in the spike script, a Put/Delete is visible to Get
before it is durable, correct me if I am wrong. What is the risk of this?
What is the alternatives
Then for the design items that we have @docs/requirement.md , for each topic,
list 2-3 options and their tradeoff in a table, plus your recommendation and
reason

## 🤖 Claude

You're right about the spike. In proto_server.py the handler does store[key] =
val and only then awaits the fsync. A Get arriving in that window returns the
new value before it is durable.

The risk. Client A's Put is applied in memory, client B reads the new value
and acts on it, then the process dies before the batch is flushed. After
restart the key has its old value. B observed a write that, from the system's
own durable history, never happened. A got no ack, so A may not retry. This is
a dirty read, and it breaks the per-key consistency requirement across a
restart: the system's history now contains a read of a value no completed
write ever produced. At the spike's flush cadence the window is 5–15 ms per
write, roughly 200 times a second, so the restart test could hit it.

Alternatives

Option: Apply after commit (recommended)
How: Handler appends the record to the batch and waits. The flusher fsyncs,
then applies the batch to the dict in log order, then wakes the waiters.
Consistency: No uncommitted value is ever readable. Memory state always equals

a durable log prefix.
Cost: Reads unchanged. Writes unchanged, the apply is a dict assignment.
Concurrent Puts to one key resolve by log order, which is also the ack order.
────────────────────────────────────────
Option: Apply early, reads wait
How: Keep apply-before-fsync but tag each key with the log offset of its last
write. A Get on a key whose offset is beyond the durable offset waits for the
 next flush.
Consistency: Correct, and reads of cold keys stay fast.
Cost: Reads of hot recently-written keys pay the flush latency. More state and

more code for no benefit over option 1 at this workload.
────────────────────────────────────────
Option: Apply early, accept dirty reads
How: Current spike behaviour.
Consistency: Violates the requirement across a crash.
Cost: None. Not acceptable given "no weaker mode".

Recommendation: apply after commit. It is the smallest change, it makes the
invariant easy to state and test, and it keeps the same structure a replicated
version would need later, where apply happens at commit as well.

Design topics from section 4

1. Wire protocol

Option: Custom length-prefixed binary over TCP
Pros: Opaque bytes with zero encoding overhead. Smallest code. Spike already
uses it and hits the numbers. Persistent connections, pipelining is trivial.
Cons: No curl debugging. Must write a client library. Hand-rolled framing is a

bug surface.
────────────────────────────────────────
Option: HTTP/1.1 with raw body (e.g. aiohttp, PUT/GET/DELETE /keys/{key})
Pros: Grader can poke it with curl. Standard semantics map cleanly to the
three
ops. Keys in the URL path need encoding.
Cons: Framework dependency and parsing overhead. Keys with arbitrary bytes
need
base64 or hex in the URL. Keep-alive handling in the load generator matters
for p99.
────────────────────────────────────────
Option: gRPC
Pros: Typed schema, generated client.
Cons: Heavy dependency, codegen step, a one-day budget does not justify it.

Recommendation: custom binary over TCP, with a small Python client library
shared by the load generator and a tiny CLI for manual checks. Reason: it is
the measured path, opaque bytes fit naturally, and a day is not enough to also
tune an HTTP stack for p99. If curl-ability matters to you, HTTP with
hex-encoded keys is the fallback.

2. Client routing to the owning server

Option: Client-side hashing: client reads the static server list and hashes
the
key itself
Pros: One hop. Simplest server. Matches spike.
Cons: Every client needs the config and the hash function. Rebalancing on
config change is a client concern (out of scope).
────────────────────────────────────────
Option: Any server forwards to the owner
Pros: Clients only need one address. Server stays authoritative about
placement.
Cons: Extra hop on 1 - 1/N of requests, roughly doubles those latencies.
Forwarding code and connection pooling on every server.
────────────────────────────────────────
Option: Separate stateless router process
Pros: Clients see one endpoint, servers stay simple.
Cons: One more process to start and stop, extra hop on every request, and the
router is a single point of failure.

Recommendation: client-side hashing in the client library, plus a cheap
WRONG_OWNER error if a server receives a key it does not own, so
misconfiguration fails loudly. Reason: it is what a static deployment allows,
and it keeps the hot path at one hop.

3. Storage layout

Option: In-memory dict + append-only log + periodic snapshot
Pros: Measured. Full control of fsync. Reads are a dict lookup. Simple replay.
Cons: Must write compaction and recovery yourself. Log grows until compacted.
Dataset must fit RAM, which is the stated assumption.
────────────────────────────────────────
Option: SQLite with synchronous=FULL, WAL mode
Pros: Durability, recovery and compaction come for free. Single file.
Cons: On macOS SQLite uses fsync, not F_FULLFSYNC, unless you set PRAGMA
fullfsync=ON. Group commit requires batching writes into one transaction
yourself. Per-transaction commit cost is a full fsync, so the same flusher
structure is needed anyway.
────────────────────────────────────────
Option: LMDB (lmdb package)
Pros: Very fast reads, ACID, no compaction.
Cons: Third-party native dependency. Same commit-per-batch structure needed.
Less transparent for the restart test.

Recommendation: dict + log + snapshot. Reason: it is already proven, the
restart test is easy to reason about, and every durability decision is visible
in the code. The one piece to add is replay and snapshot, covered in topic 5.

4. Python runtime model

Option: Single asyncio process, fsync in a thread executor
Pros: Measured at 14% CPU for 1K ops/s, 2K also passes. No locks needed for
the
dict, so per-key ordering is trivial.
Cons: One core. A slow handler stalls everyone.
────────────────────────────────────────
Option: Thread per connection with a lock around the dict and log
Pros: Familiar model.
Cons: GIL means no more throughput than asyncio, plus locking and a harder
ordering story.
────────────────────────────────────────
Option: Multiprocess, one process per core sharing a port
Pros: Scales CPU.
Cons: Shared state across processes needs IPC or sharding again. Pointless at
14% CPU.

Recommendation: single asyncio process per server. Reason: it meets the
targets with room, and single-threaded apply gives per-key ordering with no
locks. The only blocking call is fsync, and it runs in the executor.

5. Log compaction and snapshot

Option: Snapshot when log exceeds a size threshold (e.g. 64 MB or N entries),
then truncate
Pros: Bounded recovery time and disk. Standard.
Cons: Snapshot writes the whole dict. At 1 GB that is seconds of IO, needs to
run off the loop or in a child process.
────────────────────────────────────────
Option: No compaction, replay the whole log on restart
Pros: Nothing to build.
Cons: Log grows ~17 GB/day at worst case; restart time grows without bound.
────────────────────────────────────────
Option: Snapshot only on clean shutdown, replay log otherwise
Pros: Fast restart in the common case.
Cons: Does nothing about unbounded growth; the SIGKILL restart test still
replays everything.

Recommendation: size-threshold snapshot using a copy of the dict written to a
temp file, fsync, atomic rename, then truncate the log past the snapshot's log
offset. Reason: it bounds both disk and recovery time, and the atomic rename
keeps recovery correct if the process dies mid-snapshot. Recovery is load
snapshot, then replay the log from the recorded offset. For the one-day
budget, implement the simplest form: snapshot in a thread while writes
continue to a new log segment. If time runs out, a documented threshold with
no automatic trigger is an acceptable v1 fallback as long as replay works.

One addition not in section 4. Log record format needs a checksum per record
so a torn write at the tail after a crash is detected and the tail is
truncated, rather than crashing replay or loading a partial value. CRC32 from
the standard library is enough.

## 👤 Prompt

Decisions:
1. Agree Apply after commit, The flusher fsyncs, then applies the batch to
the dict in log order, then wakes the waiters. and if fsync fails, do not
apply the change
2. Wire protocal:  HTTP/1.1, easier to debug with curl. Key goes in the URL
path as base64url; value is the raw request/response body (no base64). Get on
a missing key -> 404; empty value -> 200 with an empty body.
3. Client routing: stick to the simple implementation. Use client-side
hashing with md5 of the key, return WRONG_OWNER if a server receives a key it
does not have
4. storage, use sqlite with synchronous=FULL and WAL mode, this can guarentee
durability. For this option, research and see if there is overhead if set
PRAGMA fullfsync=ON
5. python run model: Single asyncio process, sqlite runs on the writer thread
6. Compaction: sqlite automatically does this
   Point out what I missed or could be potential wrong

## 🤖 Claude

Let me measure the SQLite question rather than guess: WAL mode,
synchronous=FULL, with and without fullfsync=ON, both per-row commits and
batched commits.

## 👤 Prompt

In the spike folder, write another test to an aiohttp + SQLite (WAL,
synchronous=FULL,
fullfsync=ON, batched transactions) server and measure p99 at 1K ops/s
with N=1 and N=3. Write the result to readme and also give me the running
command

## 🤖 Claude

I'll create a venv inside spike/ to keep aiohttp isolated, then write the
server and client.

Smoke test passes for all the agreed semantics. Now the measured runs: N=1 and
N=3 at 1K ops/s, 30 s each.

Both configurations pass with margin. Updating the README with the SQLite
fullfsync numbers and the new results, and copying the SQLite micro-benchmark
into the spike folder.

The aiohttp + SQLite prototype is in spike/, measured, and documented. Not
committed, per the earlier pattern of you verifying first.

Run commands

cd spike
python3 -m venv .venv && .venv/bin/pip install aiohttp   # one-time
python3 sqlite_bench.py          # fullfsync overhead micro-benchmark
./run_http.sh 1 1000 30          # servers, ops/s, seconds
./run_http.sh 3 1000 30

Results at 1K ops/s, 30 s, open-loop, 32 connections per server

┌─────────┬─────────┬─────────┬─────────┬──────────────┬────────────────┐
│ Servers │ Get p99 │ Put p99 │ Del p99 │ Bad statuses │ CPU per server │
├─────────┼─────────┼─────────┼─────────┼──────────────┼────────────────┤
│ 1       │ 0.85 ms │ 11.3 ms │ 11.0 ms │ 0            │ 20%            │
├─────────┼─────────┼─────────┼─────────┼──────────────┼────────────────┤
│ 3       │ 0.81 ms │ 18.2 ms │ 17.3 ms │ 0            │ 1–12%          │
└─────────┴─────────┴─────────┴─────────┴──────────────┴────────────────┘

Both pass the 50 ms read and 100 ms write targets with large margin.

fullfsync=ON overhead (SQLite 3.53.2, WAL, synchronous=FULL)

┌───────────┬─────────────────┬────────────┬──────────────┐
│ fullfsync │ rows per commit │ commit p50 │ per-row cost │
├───────────┼─────────────────┼────────────┼──────────────┤
│ OFF       │ 1               │ 0.06 ms    │ 0.12 ms      │
├───────────┼─────────────────┼────────────┼──────────────┤
│ ON        │ 1               │ 4.13 ms    │ 4.61 ms      │
├───────────┼─────────────────┼────────────┼──────────────┤
│ ON        │ 20              │ 5.89 ms    │ 0.32 ms      │
└───────────┴─────────────────┴────────────┴──────────────┘

With fullfsync OFF, SQLite on macOS is not durable against power loss, the
same trap as os.fsync. ON costs one F_FULLFSYNC per commit, about 4 ms, and
batching amortizes it to 0.3 ms per row. The requirement must say fullfsync=ON
and checkpoint_fullfsync=ON explicitly.

What the prototype implements from your decisions
- HTTP/1.1 via aiohttp. Key as base64url in the path, raw body as value. 404
  on missing, 200 with empty body for empty value. Verified with curl,
  commands are in the README.
- Client-side md5 routing. Server returns 421 WRONG_OWNER for a key it does
  not own.
- One writer thread drains a queue, one transaction per batch of up to 256
  ops, applies to the dict only after COMMIT, and wakes waiters. A failed
  commit rolls back, applies nothing, and returns 500.

Things still open from the earlier question you asked, which I will fold into
the design review:
- Restart replay in the prototype is only a full table load at startup. The
  SIGKILL-mid-load test from requirement A8 has not been run yet.
- WAL checkpoints with checkpoint_fullfsync=ON pay their own full fsync. Their
  effect on tail latency was not isolated. Default auto-checkpoint is every
  1000 pages, roughly every 4 MB.
- Uneven CPU across the three servers in the N=3 run is the skewed key
  distribution landing hot keys on one owner, not a bug.

Files added: http_server.py (spike/http_server.py), http_client.py
(spike/http_client.py), run_http.sh (spike/run_http.sh), sqlite_bench.py
(spike/sqlite_bench.py), and the README part 2 section. A root .gitignore now
excludes the venv and data directories.

## 👤 Prompt

result is good. we should use http and sqlite. Commit newly added change
included the takehome md

## 🤖 Claude

Committed as 1ffe620. The working tree is clean.

The commit includes the aiohttp + SQLite server, client, runner, the SQLite
benchmark, the README part 2 section, the root .gitignore, and
takehome_assignment_prompt.md. I also saved the confirmed design decisions to
memory so later sessions do not reopen the protocol or storage choice.

Next step is the design doc, which should also resolve the two items still
open from your earlier question: the SIGKILL restart test and the effect of
WAL checkpoints on tail latency.

## 👤 Prompt

Go back to the design decisions

Decisions:
1. Agree Apply after commit, The flusher fsyncs, then applies the batch to
the dict in log order, then wakes the waiters. and if fsync fails, do not
apply the change
2. Wire protocal:  HTTP/1.1, easier to debug with curl. Key goes in the URL
path as base64url; value is the raw request/response body (no base64). Get on
a missing key -> 404; empty value -> 200 with an empty body.
3. Client routing: stick to the simple implementation. Use client-side
hashing with md5 of the key, return WRONG_OWNER if a server receives a key it
does not have
4. storage, use sqlite with synchronous=FULL and WAL mode, this can guarentee
durability.
5. python run model: Single asyncio process, sqlite runs on the writer thread
6. Compaction: sqlite automatically does this
   Point out what I missed or could be potential wrong

## 🤖 Claude

Your six decisions hold up. Here is what is missing or risky, ordered by
severity.

Must fix

1. Decision 4 is not durable on macOS as written. synchronous=FULL in WAL mode
   fsyncs the WAL on every commit, but on macOS fsync only reaches the drive
   cache. The spike measured this: 0.06 ms commits with fullfsync=OFF. Add
   PRAGMA fullfsync=ON and PRAGMA checkpoint_fullfsync=ON. Both are no-ops on
   Linux, so the same code is correct on both.
2. Decision 1 should be fail-stop, not fail-continue. After a failed COMMIT
   the OS page cache may hold data that never reached disk, and SQLite's own
   state after an I/O error is only trustworthy after reopening. Returning 500
   for the batch is right, but then exit the process. A restart reloads from
   the last durable commit, which is the state you want. Continuing risks a
   later batch committing on top of an uncertain tail.
3. The in-memory dict is a cache, so it must be loaded only from committed
   state. Startup loads all rows, then the writer applies deltas after each
   COMMIT. Nothing else may write the dict. Worth stating as an invariant in
   the design doc: memory equals the committed prefix of the database at all
   times.

Likely to bite

4. Auto-checkpoint runs inside a COMMIT on the writer thread. When the WAL
   exceeds 1000 pages SQLite checkpoints synchronously during that commit,
   with its own full fsync of the main db file. That is a periodic latency
   spike on whichever batch triggers it. At 200 writes/s of 1 KiB it fires
   about every 20 s. The spike's 30 s runs probably included one, and p99
   still passed, but the design should either measure it explicitly or run
   PRAGMA wal_checkpoint(PASSIVE) from a timer when the queue is idle.
5. Checkpoint starvation. A connection holding an open read transaction blocks
   checkpoints and the WAL grows without bound. Since reads come from the
   dict there should be no reader connection at all after startup. Close the
   loading connection, and never open a second connection on the event loop.
6. No backpressure on the write queue. If the disk stalls, the queue grows
   without bound and every waiting request eventually times out together.
   Bound the queue, around 10K, and return 503 when it is full.
7. "SQLite compacts automatically" is half true. It reuses freed pages, so the
   file does not grow from overwrite churn, but it never shrinks after
   deletes without VACUUM. Fine for this workload, but say so rather than
   claim compaction.
8. The WAL and SHM files are part of the database. Start/stop scripts and the
   restart test must never delete kv.db-wal or kv.db-shm; deleting the WAL
   loses committed data.

Decision 2 details to pin down

- Status codes beyond what you listed: 400 for a key that fails base64url
  decoding, is empty, or exceeds 256 bytes after decoding. 413 for a value
  over 64 KiB, which needs aiohttp's client_max_size set, since its default is
  1 MiB. 421 for WRONG_OWNER, with the correct owner index in the body for
  debugging. 503 for queue full, 500 for commit failure.
- PUT returns 200 both for create and replace. No 201, since the client cannot
  tell and does not need to.
- GET returns Content-Type: application/octet-stream.
- Add GET /health so start scripts can wait for readiness before load, and so
  the workload generator does not count startup time as latency.
- Keep-alive is mandatory for p99. The client library must use a connection
  pool. A fresh TCP connection per request would roughly double read latency.

Decision 3 details

- Client and servers must agree on N, the hash, and the index order. Put the
  server list in one config file that both sides read, and have the server
  take its index from that file rather than from a separate flag, so the two
  cannot drift.
- md5(key) % N with the skewed key distribution puts hot keys on one server.
  The N=3 spike showed one server at 12% CPU and another at 0.7%. Not a
  correctness issue, but say it is expected.

Decision 5 details

- Memory: 1M keys at 1 KiB in Python dict is about 1.2 GB per server with
  object overhead. Fits the RAM assumption but should be written down.
- Startup load time: a full table scan into the dict is a few seconds at 1M
  rows. The health endpoint should return not-ready until the load finishes.
- Graceful shutdown on SIGTERM: stop accepting, drain the queue, commit, exit.
  Not needed for correctness since WAL recovery handles SIGKILL, but it makes
  the stop script clean.

Missing topics

- The SIGKILL restart test from requirement A8 is still unrun. SQLite WAL
  recovery should handle it, but the verification is the point of the
  requirement.
- Server-side latency logging from requirement A4 is not in any decision yet.
- The workload generator needs to handle 421 as a configuration error, not a
  retry, and its consistency checker needs the per-key last-acked-value
  tracking.
- Config file format, port assignment, data directory layout, and pid files
  for the stop script.

Nothing here reverses a decision. Items 1 and 2 change the durability story
and should go into the requirement doc as well as the design.

## 👤 Prompt

Accept:
1. fullfsync=ON and checkpoint_fullfsync=ON
2. fail-stop — on commit failure, fail the batch and exit.
3. status code you suggested
4. in-memory dict should be loaded from the commited state
   Other is out of scope for v1

Now write docs/design.md including
1. architecture diagram
2. Put/Get sequence diagrams
3. API and config
4. recovery
5. test strategy
   Update @docs/requirement.md  to match.

## 🤖 Claude

Writing docs/design.md and updating the requirement doc to match the accepted
decisions.

Both documents are written. Not committed, pending your review.

docs/design.md contains:

1. Overview table of the seven decisions with the reason for each, including
   fullfsync, apply-after-commit, and fail-stop.
2. Architecture diagram (Mermaid) showing client library with md5 routing and
   keep-alive pool, and per server the aiohttp handlers, write queue, writer
   thread, committed-state dict, and SQLite files.
3. Put and Get sequence diagrams, with the commit-failure branch on Put and
   the point that Get never touches SQLite or the writer.
4. API table with the agreed status codes: 200, 404, 400, 413, 421 with owner
   index, 500.
5. Configuration: a single cluster.json shared by servers and clients so N and
   ordering cannot drift, the SQLite pragmas, and what the start/stop scripts
   do and must never delete.
6. Startup and recovery: load order (open and WAL replay, dict load, writer
   thread, then listen) and a table of what survives SIGTERM, SIGKILL, power
   loss, commit error, and disk loss.
7. Test strategy table: unit, integration, consistency checker, the A8 SIGKILL
   restart test, performance against the spike baseline, and a startup
   assertion that fullfsync really is on.
8. Out of scope list for v1.

docs/requirement.md changes:
- Language row now cites the spike instead of asking for the check.
- Two new must-level rows: visibility (apply-after-commit) and failure policy
  (fail-stop).
- Durability row and A2 decision name the pragmas and the group-commit choice
  with the measured numbers.
- Section 4 is now a resolved-decisions table pointing to the design doc, with
  the remaining deferred items listed.

One judgment call to flag: since the health endpoint is out of scope,
readiness is defined as "the port accepts connections", and the server opens
its port only after the dict is fully loaded. The start script waits on the
port.

## 👤 Prompt

Design overall is good. Fix these items
1. Remove the items I mark as out of scope, e.g. Zipfian keys (we should use
uniform keys, and keep 80/15/5 and use a open loop), consistency checker and
graceful drain on SIGTERM. No needt o log server-side handler latency. Update
the A4 and A5 accordingly @docs/requirement.md
2. Add another section state the linearization part, including what happended
for put/delete for the same key; and a get issued after a 200 put (it should
see the latest write), an acked write after restart; concurrent Put/Delete
on the same key: applied in log order, no application-level locks; reads
never go backwards: if a writer commits 1 and a reader sees 1, every later
reader also sees 1 (or a later write);
3. the put request flow in section 3 has some issues as below, there is no
diagram showing, check the grammar issue
   Error: Parse error on line 15:
   ...NSERT OR REPLACE ...; COMMIT (WAL fsync
   -----------------------^
   Expecting '()', 'SOLID_OPEN_ARROW', 'DOTTED_OPEN_ARROW', 'SOLID_ARROW',
'SOLID_ARROW_TOP'

## 🤖 Claude

The Mermaid error is the semicolons in the SQL message text; Mermaid treats ;
as a statement terminator. Fixing that plus the scope removals and the new
linearizability section.

Good news—the semicolon was the actual issue, not the parentheses. I'll still
strip the remaining parentheses from the Put and Get diagrams as a precaution
since I can't locally render Mermaid to verify. · summary

Line 30 is prose outside the diagram. Line 12 is md5(key), which is valid
Mermaid message text, but I'll make it plain anyway so nothing in either
diagram depends on parser tolerance.

All three fixes are applied. Not committed, pending your review.

1. Scope removals in design.md (docs/design.md) and requirement.md
(docs/requirement.md):
- Workload generator now uses uniform keys over 100K, keeps 80/15/5 and
  open-loop. The consistency checker row is removed from the test strategy and
  from A5.
- Server-side latency logging is removed from the test strategy and from A4.
  Latency is client-measured only.
- Graceful drain on SIGTERM is removed. The recovery table now treats SIGTERM
  like a crash: acked writes kept, in-flight writes lost, WAL recovery on
  open.
- The out-of-scope lists in both docs name these items explicitly.

2. New section 4, "Per-key linearizability" in the design doc. It states the
guarantee and the three mechanisms (single writer thread, FIFO queue,
apply-after-commit, no application-level locks), then a table of scenarios
with what happens and why it holds:
- Put then Get after the 200, and Delete then Get after the 200.
- Concurrent Put/Put and Put/Delete on one key: both acked, last in queue
  order wins, Delete is an ordinary record.
- Get concurrent with an in-flight Put: old or new value, never uncommitted.
- Reads never go backwards: the dict is mutated only by the writer in queue
  order and is never rolled back.
- Acked write then restart: WAL replay plus committed-table load.
- Batch commit failure: 500 for all, nothing visible, process exits.

Later sections are renumbered 5 through 9.

3. Mermaid parse error. The cause was the semicolons in the SQL text of the
Put diagram; Mermaid reads ; as a statement terminator. That line now reads
"BEGIN, INSERT OR REPLACE x batch, COMMIT with F_FULLFSYNC of the WAL". I also
removed every parenthesis from message and participant text in both sequence
diagrams so nothing relies on parser tolerance. I could not render Mermaid
locally, so please confirm the diagram shows in your preview.

## 👤 Prompt

<ide_selection>The user selected the lines 1 to 251 from
/Users/Carl_Guo/Desktop/Distributed_kv_store/docs/design.md:
# Design: Distributed Key-Value Store (v1)

Scope and constraints: [requirement.md](requirement.md). Measurements behind
the choices:
[../spike/README.md](../spike/README.md).

## 1. Overview

v1 is N independent storage servers (N = 1..4), each owning a disjoint slice
of the key space.
There is no replication. A client library hashes each key to its owner and
talks to that server
over HTTP/1.1. Each server is one asyncio process with a SQLite database on
disk and a full
in-memory copy of the committed state for reads. Writes are batched into one
SQLite
transaction per batch on a single writer thread; a request is acknowledged
only after that
transaction has committed with a full fsync.

| Decision | Choice | Reason |
|----------|--------|--------|
| Language / runtime | Python 3, one asyncio process per server | Measured
14–20% CPU at 1K ops/s; single-threaded apply gives per-key ordering without
locks |
| Wire protocol | HTTP/1.1 via aiohttp, key as base64url in the path, raw
body as value | curl-debuggable; measured p99 equal to raw TCP |
| Routing | Client-side `md5(key) % N` over a static server list | One hop;
no router process |
| Storage | SQLite, WAL, `synchronous=FULL`, `fullfsync=ON`,
`checkpoint_fullfsync=ON` | Durability, crash recovery and space reuse come
from SQLite; fullfsync is required on macOS |
| Durability | One transaction per batch (group commit); ack after COMMIT |
Per-write fullfsync costs ~4 ms; batching brings it to ~0.3 ms per write |
| Visibility | Apply to the in-memory dict only after COMMIT
(apply-after-commit) | No client can read a value that could vanish on
restart |
| Failure policy | On COMMIT failure: fail the batch with 500, exit the
process | Disk state after an fsync error is uncertain; restart reloads the
committed state |

## 2. Architecture

```mermaid
flowchart LR
    subgraph Clients
        WG[Workload generator]
        CLI[curl / CLI]
    end
    subgraph Lib[Client library]
        H["owner = md5(key) % N"]
        POOL[HTTP keep-alive pool]
    end
    WG --> H --> POOL
    CLI -. direct HTTP .-> S0

    subgraph S0[Server 0 : one asyncio process]
        direction TB
        A0[aiohttp handlers] -->|GET| D0[(in-memory dict<br/>committed
state)]
        A0 -->|PUT / DELETE| Q0[write queue]
        Q0 --> W0[writer thread<br/>batch -> BEGIN..COMMIT]
        W0 -->|after COMMIT| D0
        W
──── (174 lines hidden) ─────────────────────────────────────────────────────
e as a crash; clients saw a timeout, never a 200 | WAL recovery on open |
| SIGKILL / crash | kept | lost; clients saw a timeout, never a 200 | WAL
recovery on open |
| Power loss | kept | lost | `synchronous=FULL` + `fullfsync=ON` flush the
WAL to the medium before COMMIT returns |
| COMMIT error at runtime | kept up to the last good commit | batch gets 500
| fail-stop, then restart reloads committed state |
| Loss of the server's disk | lost | lost | out of scope for v1 (no
replication) |

Checkpointing: SQLite auto-checkpoints when the WAL passes 1000 pages, inside
a COMMIT on the
writer thread. With `checkpoint_fullfsync=ON` that commit pays an extra full
fsync. Since no
connection holds a read transaction after startup, checkpoints always
complete and the WAL
stays bounded.

## 8. Test strategy

| Level | What | How |
|-------|------|-----|
| Unit | key decoding, length limits, owner check, status codes, empty value
vs absent | pytest against the aiohttp app with `aiohttp.test_utils` |
| Unit | writer thread: batch applied to dict only after COMMIT, nothing
applied and process exits on a forced COMMIT failure | inject a failing
SQLite connection |
| Integration | three servers from `cluster.json`, client library routes
Put/Get/Delete, 421 on misrouted key | start via `scripts/start.sh`, drive
with the client library |
| Restart (A8) | acked writes survive SIGKILL mid-load | run the generator,
SIGKILL one server during writes, restart it, verify every acked Put is
readable and every acked Delete returns 404; `-wal` and `-shm` files are left
in place |
| Performance | 1K ops/s, 80/15/5, p99 read < 50 ms and write < 100 ms |
open-loop generator at 1,000 ops/s for 60 s against N=1 and N=3; report
p50/p95/p99/p999 measured client-side; spike baseline: put p99 11 ms (N=1),
18 ms (N=3) |
| Durability pragma | `fullfsync` really on | startup asserts `PRAGMA
fullfsync` returns 1 and `PRAGMA synchronous` returns 2 |

Workload generator (`kvbench`): open-loop at a fixed arrival rate, 32
keep-alive connections
per server, uniform keys over 100K, 80% Get / 15% Put / 5% Delete, 1 KiB
values, histogram
report printed and saved as CSV.

## 9. Out of scope for v1

Replication and consensus, failover, partition handling, write-queue
backpressure, health
endpoint, graceful drain on SIGTERM, consistency checker in the workload
generator, server-side
latency logging, VACUUM, metrics endpoint, TLS, auth.


This may or may not be related to the current task.</ide_selection>

Design approved. Commit the current change

## 🤖 Claude

Committed as 2dd16d3. The working tree is clean.

The commit holds the approved design.md (docs/design.md) and the matching
updates to requirement.md (docs/requirement.md). Next step is implementation:
the server, client library, workload generator, start/stop scripts, and the
tests from section 8 of the design, plus saving the coding-agent prompts as a
deliverable.

## 👤 Prompt

Now work on an implementation of v1 from @docs/design.md  and
@docs/requirement.md , it should be self-contained

For the implementation plan
1. Do not change the @docs/design.md  and @docs/requirement.md
2. build milestone in order, each should be small module to be done and
finished, for example, stoarge, HTTP server, client lib, script, workload
generator
3. For each milestone, use a test first development (TDD).
4. For final test, use the workload generator with n=1 and n=3 to run 1k
ops/s and meet the p99 target, save the result in readme

Now write the implementation plan in doc

## 🤖 Claude

The writing-plans skill applies here, so I'll load it before drafting the
plan.

I'm using the writing-plans skill to create the implementation plan. I'll
write it to docs/plans/2026-10-25-kv-v1-implementation.md, leaving design.md
and requirement.md untouched.

Plan complete and saved to docs/plans/2026-10-25-kv-v1-implementation.md. Not
committed yet.

Structure: 11 tasks in 10 milestones, each test-first with the failing test,
the exact run command and expected output, the full implementation code, and a
commit step.

┌───────────────┬──────┬──────────────────────────────────────────────────┐
│   Milestone   │ Task │                     Produces                     │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│ M0            │ 1    │ venv, package, pytest config                     │
│ Scaffolding   │      │                                                  │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│ M1 Config and │ 2, 3 │ config.py with load_config and owner_of,         │
│  key codec    │      │ keycodec.py with limits                          │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│               │      │ storage.py: writer thread, batched commits,      │
│ M2 Storage    │ 4    │ apply-after-commit, fail-stop; tests inject      │
│               │      │ blocking, counting and failing COMMIT wrappers   │
│               │      │ to prove each property                           │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│ M3 HTTP       │ 5    │ server.py with every status code from design     │
│ server        │      │ section 5 tested                                 │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│ M4 Client     │ 6    │ client.py with md5 routing, WrongOwner on 421    │
│ library       │      │                                                  │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│ M5 Scripts    │ 7    │ start.sh and stop.sh, tested by bringing up a    │
│               │      │ real 3-server cluster                            │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│ M6 Workload   │ 8    │ bench.py: open-loop, 80/15/5, uniform keys,      │
│ generator     │      │ percentile report, pass/fail exit code, CSV      │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│ M7 Restart    │ 9    │ SIGKILL mid-load, restart, verify every acked    │
│ test          │      │ write                                            │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│ M8 Final      │ 10   │ N=1 and N=3 at 1K ops/s for 60 s, results in     │
│ measurement   │      │ README                                           │
├───────────────┼──────┼──────────────────────────────────────────────────┤
│ M9 Prompts    │ 11   │ prompts/README.md for requirement F12            │
└───────────────┴──────┴──────────────────────────────────────────────────┘

Points worth knowing before execution:
- Storage futures are concurrent.futures.Future resolved on the writer thread.
  The server bridges them with asyncio.wrap_future, so storage is testable
  without asyncio.
- The plan forbids editing design.md and requirement.md, and says to stop and
  report if the code must deviate.
- The restart test is marked as a real durability check: the plan says not to
  weaken it if it fails.

Two execution options:

1. Subagent-driven, this session. I dispatch a fresh subagent per task, review
   each result against the plan, and keep going. Faster iteration, and I keep
   the full context.
2. Parallel session. You open a new session that uses the executing-plans
   skill and runs the tasks in batches with checkpoints.

Which approach, and should I commit the plan first?

## 👤 Prompt

Remove task 11
For task 9, there could be a situation that PUT commit but the server is
killed before sending the 200. The client records nothing, but after the
restart GET returns the new value. To fix this potential issue, Each writer
stops at its first error, so each key has at most one pending (un-acked)
operation; Record per key the last acked value and that pending operation (a
pending Delete means NotFound is also valid); Assert the Get result equals
the acked value or the pending value. Only a result that is neither is a bug.

Fix these and then commit

## 🤖 Claude

One simplification to the validity logic before committing: a key with no
acked state is valid as absent regardless of the pending op type, so the
branch can be plain.

Committed as d3add08.

Changes to the plan:

- Task 11 removed. The milestone table, layout listing, README template,
  implementer rules and verification checklist no longer mention the prompts
  deliverable. Ten tasks remain.
- Task 9 rewritten for the commit-then-killed case. Each writer records its
  operation as pending before sending it, promotes it to acked on response,
  and stops at its first error. That guarantees at most one pending operation
  per key. Verification builds the valid set per key as the acked value, or
  absent if the key was never acked, plus the pending value if one exists,
  where a pending Delete makes NotFound valid. A Get that returns anything
  outside that set is the only failure. The test also asserts there are at
  most eight pending keys, one per writer.

The plan is ready to execute. Tell me whether you want subagent-driven
execution in this session or a separate session with the executing-plans
skill.
