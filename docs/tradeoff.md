**Alternatives**

| Option | How | Consistency | Cost |
|--------|-----|-------------|------|
| Apply after commit (recommended) | Handler appends the record to the batch and waits. The flusher fsyncs, then applies the batch to the dict in log order, then wakes the waiters. | No uncommitted value is ever readable. Memory state always equals a durable log prefix. | Reads unchanged. Writes unchanged, the apply is a dict assignment. Concurrent Puts to one key resolve by log order, which is also the ack order. |
| Apply early, reads wait | Keep apply-before-fsync but tag each key with the log offset of its last write. A Get on a key whose offset is beyond the durable offset waits for the next flush. | Correct, and reads of cold keys stay fast. | Reads of hot recently-written keys pay the flush latency. More state and more code for no benefit over option 1 at this workload. |
| Apply early, accept dirty reads | Current spike behaviour. | Violates the requirement across a crash. | None. Not acceptable given "no weaker mode". |

Recommendation: apply after commit. It is the smallest change, it makes the invariant easy to state and test, and it keeps the same structure a replicated version would need later, where apply happens at commit as well.

**Design topics from section 4**

**1. Wire protocol**

| Option | Pros | Cons |
|--------|------|------|
| Custom length-prefixed binary over TCP | Opaque bytes with zero encoding overhead. Smallest code. Spike already uses it and hits the numbers. Persistent connections, pipelining is trivial. | No curl debugging. Must write a client library. Hand-rolled framing is a bug surface. |
| HTTP/1.1 with raw body (e.g. aiohttp, PUT/GET/DELETE /keys/{key}) | Grader can poke it with curl. Standard semantics map cleanly to the three ops. Keys in the URL path need encoding. | Framework dependency and parsing overhead. Keys with arbitrary bytes need base64 or hex in the URL. Keep-alive handling in the load generator matters for p99. |
| gRPC | Typed schema, generated client. | Heavy dependency, codegen step, a one-day budget does not justify it. |

Recommendation: custom binary over TCP, with a small Python client library shared by the load generator and a tiny CLI for manual checks. Reason: it is the measured path, opaque bytes fit naturally, and a day is not enough to also tune an HTTP stack for p99. If curl-ability matters to you, HTTP with hex-encoded keys is the fallback.

**2. Client routing to the owning server**

| Option | Pros | Cons |
|--------|------|------|
| Client-side hashing: client reads the static server list and hashes the key itself | One hop. Simplest server. Matches spike. | Every client needs the config and the hash function. Rebalancing on config change is a client concern (out of scope). |
| Any server forwards to the owner | Clients only need one address. Server stays authoritative about placement. | Extra hop on 1 - 1/N of requests, roughly doubles those latencies. Forwarding code and connection pooling on every server. |
| Separate stateless router process | Clients see one endpoint, servers stay simple. | One more process to start and stop, extra hop on every request, and the router is a single point of failure. |

Recommendation: client-side hashing in the client library, plus a cheap `WRONG_OWNER` error if a server receives a key it does not own, so misconfiguration fails loudly. Reason: it is what a static deployment allows, and it keeps the hot path at one hop.

**3. Storage layout**

| Option | Pros | Cons |
|--------|------|------|
| In-memory dict + append-only log + periodic snapshot | Measured. Full control of fsync. Reads are a dict lookup. Simple replay. | Must write compaction and recovery yourself. Log grows until compacted. Dataset must fit RAM, which is the stated assumption. |
| SQLite with `synchronous=FULL`, WAL mode | Durability, recovery and compaction come for free. Single file. | On macOS SQLite uses `fsync`, not `F_FULLFSYNC`, unless you set `PRAGMA fullfsync=ON`. Group commit requires batching writes into one transaction yourself. Per-transaction commit cost is a full fsync, so the same flusher structure is needed anyway. |
| LMDB (`lmdb` package) | Very fast reads, ACID, no compaction. | Third-party native dependency. Same commit-per-batch structure needed. Less transparent for the restart test. |

Recommendation: dict + log + snapshot. Reason: it is already proven, the restart test is easy to reason about, and every durability decision is visible in the code. The one piece to add is replay and snapshot, covered in topic 5.

**4. Python runtime model**

| Option | Pros | Cons |
|--------|------|------|
| Single asyncio process, fsync in a thread executor | Measured at 14% CPU for 1K ops/s, 2K also passes. No locks needed for the dict, so per-key ordering is trivial. | One core. A slow handler stalls everyone. |
| Thread per connection with a lock around the dict and log | Familiar model. | GIL means no more throughput than asyncio, plus locking and a harder ordering story. |
| Multiprocess, one process per core sharing a port | Scales CPU. | Shared state across processes needs IPC or sharding again. Pointless at 14% CPU. |

Recommendation: single asyncio process per server. Reason: it meets the targets with room, and single-threaded apply gives per-key ordering with no locks. The only blocking call is fsync, and it runs in the executor.

**5. Log compaction and snapshot**

| Option | Pros | Cons |
|--------|------|------|
| Snapshot when log exceeds a size threshold (e.g. 64 MB or N entries), then truncate | Bounded recovery time and disk. Standard. | Snapshot writes the whole dict. At 1 GB that is seconds of IO, needs to run off the loop or in a child process. |
| No compaction, replay the whole log on restart | Nothing to build. | Log grows ~17 GB/day at worst case; restart time grows without bound. |
| Snapshot only on clean shutdown, replay log otherwise | Fast restart in the common case. | Does nothing about unbounded growth; the SIGKILL restart test still replays everything. |

Recommendation: size-threshold snapshot using a copy of the dict written to a temp file, fsync, atomic rename, then truncate the log past the snapshot's log offset. Reason: it bounds both disk and recovery time, and the atomic rename keeps recovery correct if the process dies mid-snapshot. Recovery is load snapshot, then replay the log from the recorded offset. For the one-day budget, implement the simplest form: snapshot in a thread while writes continue to a new log segment. If time runs out, a documented threshold with no automatic trigger is an acceptable v1 fallback as long as replay works.

**One addition not in section 4.** Log record format needs a checksum per record so a torn write at the tail after a crash is detected and the tail is truncated, rather than crashing replay or loading a partial value. CRC32 from the standard library is enough.