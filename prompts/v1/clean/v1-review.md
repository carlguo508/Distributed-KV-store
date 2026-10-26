# v1-review

## 👤 Prompt

You are a project review engineer, Read the @docs/requirement.md and
@docs/design.md to understand this project and then read the
@docs/plans/2026-10-25-kv-v1-implementation.md  and see if there is any
invalid plan, pay more attention to the linearization model and restart
recovery of the system

## 🤖 Claude

I'll read the implementation plan and check it against the requirements and
design, focusing on linearizability and restart recovery.

I read all three documents. Overall the plan faithfully implements the design,
and the core linearizability mechanism (single FIFO writer,
apply-after-commit, ack after apply) is sound. I found one real flaw in the
restart test, one design/plan mismatch in the fail-stop path, one bug that
will make a unit test fail, and a few smaller gaps.

Verdict

The architecture in the plan holds up. Per-key linearizability follows from
the FIFO queue, the single writer thread, and the fact that the dict is
mutated and the future resolved only after COMMIT. Startup order (load, then
start writer, then listen) matches design section 7. The problems below are in
the tests and edge paths, not in the model itself.

Findings

1. The restart test will fail spuriously on a valid history (high).
2026-10-25-kv-v1-implementation.md:1704-1738
(docs/plans/2026-10-25-kv-v1-implementation.md#L1704-L1738)

Each writer records a key's value only when the request is acked. At the
moment of SIGKILL, each of the 8 writers has one request in flight. If the
server died after COMMIT and dict apply but before the HTTP response was
written, that write is durable but unacked. After restart, Get returns the
newer unacked value, but the test asserts strict equality with the last acked
value and fails. Under linearizability an unacked write is allowed to take
effect, so this is a false positive, not a durability bug. The fsync window is
about 4 ms versus roughly 0.1 ms to write the response, so across 8 in-flight
writes this will flake on a noticeable fraction of runs. The plan's
instruction on line 1747, "if it fails, the failure is a real durability bug:
do not weaken the test," would then send the implementer chasing a ghost.

Fix: record per key both the last acked value and the single pending unacked
operation issued after it. Assert the Get result equals the acked value or the
pending value. Only a result that is neither is a bug. The test should also
check that kv.db-wal and kv.db-shm are left in place, as design section 8
requires.

2. Production never sends the promised 500 on commit failure (medium).
2026-10-25-kv-v1-implementation.md:614-616
(docs/plans/2026-10-25-kv-v1-implementation.md#L614-L616) and :716-718
(docs/plans/2026-10-25-kv-v1-implementation.md#L716-L718)

The writer sets the exceptions on the futures, then calls the default
on_fatal, which calls os._exit(1) immediately on the writer thread. The event
loop never gets to run the resumed handlers, so clients see a connection
reset, not a 500. Design sections 4 and 5 and the plan's own Task 5 test
promise a 500, but every test injects a non-exiting on_fatal, so the gap is
invisible. Safety is unaffected because nothing was applied and the client
sees an error either way. Either change the design wording to "connection
closed or 500", or have server.py pass an on_fatal that schedules the exit on
the loop after a short delay so responses flush first.

3. The percentile formula fails its own test (medium, not linearizability).
2026-10-25-kv-v1-implementation.md:1493-1497
(docs/plans/2026-10-25-kv-v1-implementation.md#L1493-L1497)

For 100 samples and p99 the index is round(99.5) - 1. Python rounds half to
even, so that is 99, which returns 100.0 while the test on line 1400 expects
99.0. Use nearest-rank with math.ceil(pct * n / 100.0) - 1, which satisfies
all four assertions. This matters because p99 is the graded number.

4. start.sh reports success even if a server never comes up (low).
2026-10-25-kv-v1-implementation.md:1317-1323
(docs/plans/2026-10-25-kv-v1-implementation.md#L1317-L1323)

If the process exits during startup (for example the durability pragma
assertion fails), the readiness loop times out after 20 s and the script still
prints "listening" and exits 0. Make the loop fail the script if the port
never opens, since the restart demo in Task 10 relies on this script.

5. Failing assertions inside injected connections kill the pytest process
(low). 2026-10-25-kv-v1-implementation.md:473-483
(docs/plans/2026-10-25-kv-v1-implementation.md#L473-L483) and :518-528
(docs/plans/2026-10-25-kv-v1-implementation.md#L518-L528)

BlockingCommit and Gate assert inside the writer thread with the default
on_fatal. If the wait times out, the assertion is caught as a commit failure
and os._exit(1) terminates the whole test run with no report. Pass
on_fatal=fatal.append in those two tests too.

6. Two documentation-level notes, no change needed.

- A Get can observe a batch half applied, for example seeing v1 for a key
  while v2 from the same batch is not yet assigned. Both writes are unacked
  and overlapping, and the dict only ever moves forward in queue order, so
  this is a valid linearization. Worth one sentence in the design's "Get
  concurrent with an in-flight Put" row.
- The design says futures are resolved via loop.call_soon_threadsafe. The plan
  uses a thread-safe concurrent.futures.Future wrapped by
  asyncio.wrap_future, which does the same thing internally. Equivalent, just
  a different spelling.

What I checked and found correct

- Ack only after COMMIT, dict apply before future resolution, so a Get issued
  after a 200 always sees the write.
- Rollback and no-apply on any failure in the batch, including statement
  errors before COMMIT.
- Writer-only mutation of the dict after load(), and the read connection is
  closed before the writer starts so checkpoints never stall.
- SIGKILL, SIGTERM, and power-loss rows in design section 7 are all covered by
  WAL plus synchronous=FULL and fullfsync=ON, with the pragmas asserted at
  startup.
- Deleting an absent key goes through the batch, so its ack carries the same
  durability meaning.

Recommended order: fix finding 1 before running Task 9, fix finding 3 before
Task 8, and decide on finding 2 before writing the README since it touches the
documented API semantics.

## 👤 Prompt

/code-review

/code-review
