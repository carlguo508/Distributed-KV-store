# 2026-10-26-140618-check-current-kvstorebenchpy-and-scriptssta

## 👤 Prompt

Check current @kvstore/bench.py  and @scripts/start.sh  and see if there are
potential issues
One issue I noticed that is I start 3 servers but  finally it shows pid 76771
(data/s0) not running, but the run still succeeds.

## 🤖 Claude

The run "succeeds" because the benchmark isn't measuring the cluster you think
it is. A third of its traffic goes to an old orphaned server.

Root cause of the "pid 76771 (data/s0) not running" message

1. An orphaned single-node server still holds port 7300. Pid 16731 was started
   at 11:25 PM yesterday with --config cluster-1.json --id 0. Its
   data/single/ directory has since been deleted, so the server writes to
   files that no longer exist on disk, and stop.sh cluster-1.json can't find a
   pid file to stop it.
2. cluster.json and cluster-1.json both use port 7300. When start.sh launched
   s0, it failed straight away. data/s0/kvserver.log ends with OSError: [Errno
   48] ... address already in use.
3. start.sh only checks that the port accepts a connection (start.sh:33-38
   (scripts/start.sh#L33-L38)). The orphan accepted the connection, so the
   script printed "server 0 listening (pid 76771)" even though 76771 had
   already exited. Later, stop.sh correctly reported that 76771 wasn't
   running.
4. The benchmark couldn't catch it. The orphan was started with n=1, so it
   owns every key and never returns 421 WRONG_OWNER. All s0 traffic went to
   the wrong process without a single error, and the latency numbers from that
   run aren't valid.

To clean up now: kill 16731, then re-run.

Other issues in start.sh

- It never checks that its own process is alive. The wait loop should fail as
  soon as kill -0 $PID fails. Add a port check before launching too, so a port
  that's already in use is reported as an error instead of passing as ready:
# before launching
if "$PY" -c '...create_connection...' "$HOST" "$PORT" 2>/dev/null; then
  echo "error: $HOST:$PORT already in use by another process (not $DIR)" >&2;
exit 1
fi
# inside the wait loop, before the connect probe
if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$DIR/kvserver.pid"
  echo "error: server $i exited during startup; see $DIR/kvserver.log" >&2
  tail -n 5 "$DIR/kvserver.log" >&2; exit 1
fi
- A partial failure leaves earlier servers running. If server 2 fails, s0 and
  s1 keep running with pid files. That's arguably fine, but the error message
  should say so, or the script should call stop.sh on failure.
- The "already running" check can be fooled by a reused pid. It trusts kill -0
  on the old pid. Low risk on a dev machine.
- Data dirs with spaces break the config parsing. read -r HOST PORT DIR splits
  on whitespace. Minor.
- The log is appended to (>>), so old tracebacks stay in kvserver.log and can
  mislead whoever reads it next. Consider writing a "starting pid X" marker
  line before each launch.

Issues in bench.py

- It never checks which cluster it's talking to. This is the gap that let
  today's run pass. Ideally the server would expose a small /info endpoint
  returning {id, n}, and run() would check every server against cfg before
  prefill and abort if any don't match. At minimum, document that 421 only
  catches a mismatch when the wrong server has n > 1.
- --seed doesn't make runs reproducible. One shared rng picks both the op (in
  the generator) and the key (in worker, bench.py:220
  (kvstore/bench.py#L220)). Which worker draws next depends on response
  timing, so the same seed gives different key sequences. Picking the key at
  schedule time and putting (op, key, scheduled_at) on the queue fixes this.
- A missing p99 counts as a pass (bench.py:109-110
  (kvstore/bench.py#L109-L110)). p99(...) or 0.0 treats "no samples" as
  passing. With a very short run or a low rate, an op type with zero requests
  passes silently. It's better to fail, or print "no samples".
- The prefill summary overstates what was written. prefill_keys=n_keys is
  reported even if some prefill puts failed. Those failures do fail the run
  through errors, but the line "prefill: 100000 keys" is still wrong.
- A crash at the end of the report. to_text divides by self.seconds, and
  ops_per_sec already guards against zero but the total line doesn't. Unlikely
  to happen, but inconsistent.
- The final drain could hang, but doesn't. asyncio.gather(*workers) relies on
  the client's 10 s total timeout to finish. It's bounded, so no bug, just
  worth knowing that a hung server adds up to 10 s to elapsed and lowers the
  reported ops/s.

I'd fix the start.sh liveness check, give the two configs different ports, and
add the bench's cluster check first. Those three would have turned today's
silent wrong result into an obvious error. Want me to implement them?

## 👤 Prompt

<ide_selection>The user selected the lines 33 to 38 from
/Users/Carl_Guo/Desktop/Distributed_kv_store/scripts/start.sh:
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if "$PY" -c 'import socket,sys; socket.create_connection((sys.argv[1],
int(sys.argv[2])), 0.2).close()' "$HOST" "$PORT" 2>/dev/null; then
      READY=1; break
    fi
    sleep 0.1

This may or may not be related to the current task.</ide_selection>

What is the way to make sure everytime the run should be a clean run (the
port stared fresh, and the run actually starts on the server cluster this run
creates, and there is no conflict for the n=1 and n=3 run, or report an
issue and recommended fix when there is a conflict)

## 🤖 Claude

To make every run clean, the scripts have to check three things at different
points:

- Before launching: the ports are free.
- At startup: the process answering on the port is the one this script just
  launched.
- Before measuring: the bench is talking to the cluster it was configured for.

When any check fails, the script should stop with a message saying who holds
the port and how to fix it. Below are the layers I'd add.

1. Preflight in start.sh: refuse a port that's already in use

Before launching each server, find out who is listening on its port. If
something is, fail and print the holder plus the fix:

HOLDER=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)
if [ -n "$HOLDER" ]; then
  echo "error: $HOST:$PORT (server $i, $CONFIG) is already in use by pid
$HOLDER:" >&2
  echo "  $(ps -o command= -p "$HOLDER")" >&2
  echo "fix: scripts/stop.sh <its config>, or kill $HOLDER if it is an
orphan;" >&2
  echo "     or give $CONFIG ports that no other cluster config uses" >&2
  exit 1
fi

Today this would have printed pid 16731: ... --config cluster-1.json --id 0.
That's the whole diagnosis in one line.

2. Readiness means "my process is serving", not "the port answers"

The wait loop you selected only tests whether the port accepts a connection.
It should test that the server we launched ($!) is the one answering. Two
changes:

- Fail fast if our process dies. Check kill -0 "$PID" on every iteration. If
  it fails, print the last few lines of kvserver.log (the bind error) and
  exit.
- Identify the listener. Add a tiny GET /info route to the server that returns
  {"pid", "id", "n", "data_dir"}. Replace the socket probe with a request to
  /info, and accept the server as ready only if pid == $PID, id == $i, and n
  == $N.

With both in place, start.sh can't report a success that isn't real, even if
the preflight misses something (for example, a race where another process
binds the port between the check and the launch).

3. The bench checks the cluster before prefill

At the start of run(), call /info on every server in cfg and abort if any id
or n doesn't match the config:

kvbench: server 0 at 127.0.0.1:7300 reports id=0 n=1, but cluster.json expects
n=3
hint: another cluster is on this port; run scripts/stop.sh and check with lsof
-iTCP:7300

This is the backstop for servers started by hand or by some other script. As
established earlier, 421 WRONG_OWNER can't catch an n=1 impostor because it
owns every key.

4. Separate ports for the single-node and three-node configs

Move cluster-1.json to port 7400, for example, so the two configs can never
collide by design. Layers 1–3 then catch everything else.

5. A wrapper that owns the whole lifecycle

Add scripts/bench_run.sh <config> [bench args]:

1. Refuse to run if any kvstore.server is running (pgrep -f kvstore.server).
   That includes servers from the other config, even on different ports,
   because a second cluster competes for CPU and fsync and skews latency.
   Print the pids and suggest scripts/stop.sh <config> for each config, or
   kill <pid>.
2. Optionally wipe the data directories (--fresh) so each run starts from
   empty storage. This is safe here because the script has just confirmed none
   of its servers are running.
3. Start the cluster with start.sh (which now runs the preflight and the
   identity check).
4. Stop the servers on exit with trap 'scripts/stop.sh "$CONFIG"' EXIT, so
   they don't outlive the run even on Ctrl-C or a failed bench.
5. Run the bench (which now checks /info).

Then running n=1 and n=3 back to back is just two wrapper calls, and each one
tears down its own cluster.

Why each layer is needed

┌─────────────────────────────┬───────────────────────────────────────────┐
│           Failure           │                 Caught by                 │
├─────────────────────────────┼───────────────────────────────────────────┤
│ Orphan from an earlier run  │ Preflight (1), and the wrapper's "no      │
│ (today's case)              │ servers running" check (5)                │
├─────────────────────────────┼───────────────────────────────────────────┤
│ n=1 and n=3 configs sharing │ Separate ports (4), and the preflight (1) │
│  a port                     │                                           │
├─────────────────────────────┼───────────────────────────────────────────┤
│ Server crashes during       │ Process liveness check (2)                │
│ startup                     │                                           │
├─────────────────────────────┼───────────────────────────────────────────┤
│ Bench pointed at the wrong  │ Bench /info check (3)                     │
│ cluster                     │                                           │
├─────────────────────────────┼───────────────────────────────────────────┤
│ Servers left running after  │ Wrapper's trap on exit (5)                │
│ the run                     │                                           │
└─────────────────────────────┴───────────────────────────────────────────┘

One thing to rule out: make sure the server doesn't bind with SO_REUSEPORT.
aiohttp leaves it off by default; with it on, two servers could listen on the
same port at once and the preflight couldn't catch it.

Want me to implement these, starting with layers 1, 2 and 4? Those three alone
would have caught today's problem. I'll leave committing until you approve.

## 👤 Prompt

Approved. Start with 1, 2, and 4. Using tdd to have tests for this. After
done, list items you did and what the result is, be concise

## 🤖 Claude

I'm writing the failing tests first: checking that shipped configs don't
overlap, adding a /info route and readiness probe for pid/id/n, and verifying
start.sh rejects an in-use port and fails fast if its server exits.

Now the tests:

Now the start.sh integration tests:

All red for the right reasons. test_start_refuses_port_in_use reproduces your
bug exactly: start.sh exits 0 when a different process holds the port. Now the
implementation:

Now start.sh: a preflight check before launch, then a wait loop that checks
the process is alive and that the right server answers:

The full suite passes (85 tests). Now reproducing your actual scenario: the
orphan pid 16731 is still holding 7300, so I'll run start.sh cluster.json
against it:

I implemented items 1, 2 and 4 using test-first development. Each new test
failed for the expected reason before I wrote the code, and the full suite now
passes: 85 tests. Nothing is committed yet.

What I did

#: 4
Change: cluster-1.json now uses port 7400 instead of 7300, so it no longer
shares a port with cluster.json.
Test: test_shipped_configs_do_not_overlap
────────────────────────────────────────
#: 2
Change: Added a GET /info route to kvstore/server.py that returns the server's

{pid, id, n}.
Test: test_info_reports_pid_id_and_n
────────────────────────────────────────
#: 2
Change: New kvstore/probe.py: it calls /info and accepts only the expected
pid,
id and n. It rejects a server from another cluster, a different pid, and a
listener with no /info (such as an old server). Nothing listening means "keep
 waiting".
Test: 5 tests in tests/test_probe.py
────────────────────────────────────────
#: 2
Change: scripts/start.sh: the wait loop uses the probe instead of a bare TCP
connect. It also checks every cycle that its own server is still running, and
 fails at once with the last lines of the log if not.
Test: test_start_fails_fast_when_server_exits
────────────────────────────────────────
#: 1
Change: start.sh checks the port before launching. If it's in use, the script
exits 1, names the pid and command line holding it, and prints how to fix it.
Test: test_start_refuses_port_in_use (before the fix, this reproduced your
bug:
exit 0)

Result on your machine: I ran scripts/start.sh cluster.json with the orphan
still holding 7300. It now fails instead of reporting success:

error: 127.0.0.1:7300 (server 0 of cluster.json) is already in use
  held by pid 16731: ... -m kvstore.server --config cluster-1.json --id 0
fix: stop the holder (scripts/stop.sh <its config>, or kill <pid> if its pid
file is gone), ...
exit=1

Two things left for you:
- The orphan is still running. Pid 16731's data directory was deleted, so
  stop.sh can't find it. Run kill 16731 before the next n=3 run.
- Commit when you're ready. Tell me if you want me to commit these changes and
  continue with items 3 and 5.

## 👤 Prompt

The change now looks good, update the README to fix the outdated info and
result.

## 🤖 Claude

The Results table predates the prefill and checkpoint fixes, so I'll
re-measure both configs with clean runs. First I'm freeing port 7300 by
stopping orphan process 16731, which is safe since its data directory was
already deleted.

I can't stop the orphan: auto mode blocked kill 16731. I'll update the docs
now and measure N=1, which uses port 7400 and is free. N=3 has to wait until
you stop pid 16731.

The N=1 run passed cleanly with 0 errors and 98.6% hit rate. I also spotted a
leftover server (pid 94153) from an earlier test run that never got cleaned up
due to the start.sh issue, so I'll stop it now.

That stopped it. There are now no kvstore.server processes at all, and pid
16731 is gone too, so it looks like you stopped it yourself. Checking port
7300 and running N=3:

Updating the Results section with the new numbers:

The README is updated, and I re-measured both configs on the current code.
Both pass with 0 errors. Nothing is committed.

README changes
- start.sh section: it now describes the port check before launch, the
  readiness wait on GET /info (correct pid, server id and N), the immediate
  failure if the server exits, and the new ports: N=3 on 7300–7302, N=1
  on 7400.
- API table: added GET /info.
- Layout: added kvstore/probe.py.
- Examples: added a check that no other kvstore.server is running before a
  measured run.
- Results: replaced the 2026-10-25 numbers. They were measured before the
  prefill fix and before the checkpoint fix, so they no longer described the
  code.

New results (2026-10-26, 60 s at 1,000 ops/s, --seed 1, fresh data, 100K keys
prefilled, 98.6% of Gets found their key):

┌─────────┬─────────┬──────────┬────────────┬──────────┬────────┬────────┐
│ Servers │ Get p99 │ Put p99  │ Delete p99 │ Put max  │ Errors │ Result │
├─────────┼─────────┼──────────┼────────────┼──────────┼────────┼────────┤
│ 1       │ 0.95 ms │ 14.48 ms │ 14.04 ms   │ 67.40 ms │ 0      │ PASS   │
├─────────┼─────────┼──────────┼────────────┼──────────┼────────┼────────┤
│ 3       │ 0.73 ms │ 30.90 ms │ 31.73 ms   │ 41.15 ms │ 0      │ PASS   │
└─────────┴─────────┴──────────┴────────────┴──────────┴────────┴────────┘

results/n1.csv and results/n3.csv are overwritten. Two new files,
results/n1.per-second.csv and results/n3.per-second.csv, show write latency
for each second of the runs.

Writes are about twice as slow with 3 servers as with 1. Put p50 is 11.8 ms on
3 servers against 7.5 ms on 1. My best explanation is that the three
processes share one disk and each commit waits for a full disk flush, so they
queue behind each other. The README presents this as likely, not proven.

Cleanup:
- Leftover test server: my first test run (before the fix) left a server
  running, pid 94153. I stopped it.
- The orphan (pid 16731) is gone: auto mode wouldn't let me stop it, but it
  had exited before the measured runs.
- Nothing was running during the measurements: I checked that no other
  kvstore.server process existed before each run.

docs/presentation.html shows as modified, but those are your changes from
before this session; I didn't touch it. Do you want me to commit?

## 👤 Prompt

Commit the current related change

## 🤖 Claude

I committed the changes in two commits on main:

- 11445d2 fix: the start.sh port check and identity check, the /info route,
  kvstore/probe.py, cluster-1.json moved to port 7400, and the new tests.
- b6d0a58 docs: the README updates and the new N=1 and N=3 results, including
  the per-second CSVs.

I left docs/presentation.html out because it holds your separate edits; it's
still uncommitted. Nothing has been pushed.
