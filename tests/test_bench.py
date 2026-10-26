import asyncio
import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from kvstore import bench
from kvstore.bench import READ_P99_MS, WRITE_P99_MS, Report, classify_error, percentile, pick_op, run
from kvstore.client import ServerError, WrongOwner
from kvstore.config import ClusterConfig, ServerConfig
from kvstore.server import create_app
from kvstore.storage import Storage


# Checks: percentiles use the nearest-rank method on sorted samples (requirement A5 histogram report).
# Setup: the samples 1..100, and an empty list.
# Action: take p50, p99 and p99.9 of 1..100, and p99 of the empty list.
# Expected: 50, 99 and 100; None for the empty list.
def test_percentile_on_sorted_samples():
    samples = sorted(float(i) for i in range(1, 101))  # 1..100
    assert percentile(samples, 50) == 50.0
    assert percentile(samples, 99) == 99.0
    assert percentile(samples, 99.9) == 100.0
    assert percentile([], 99) is None


# Checks: the operation mix is 80% Get / 15% Put / 5% Delete (requirement A5).
# Setup: uniform random numbers at the edges of each band.
# Action: map each to an operation.
# Expected: [0, 0.80) is get, [0.80, 0.95) is put, [0.95, 1) is delete.
def test_pick_op_follows_80_15_5():
    assert pick_op(0.0) == "get" and pick_op(0.79) == "get"
    assert pick_op(0.80) == "put" and pick_op(0.94) == "put"
    assert pick_op(0.95) == "delete" and pick_op(0.999) == "delete"


# Checks: pass/fail uses read p99 < 50 ms, write p99 < 100 ms, and zero errors (requirement §2 Performance).
# Setup: four reports: all fast, slow reads, slow writes, and fast but with one error.
# Action: call passed() on each.
# Expected: only the all-fast, error-free report passes.
def test_report_pass_fail_uses_targets():
    ok = Report(latencies={"get": [1.0] * 100, "put": [2.0] * 100, "delete": [2.0] * 100}, seconds=1, errors=0)
    assert ok.passed()
    slow_read = Report(latencies={"get": [READ_P99_MS + 1] * 100, "put": [1.0], "delete": [1.0]}, seconds=1, errors=0)
    assert not slow_read.passed()
    slow_write = Report(latencies={"get": [1.0], "put": [WRITE_P99_MS + 1] * 100, "delete": [1.0]}, seconds=1, errors=0)
    assert not slow_write.passed()
    errored = Report(latencies={"get": [1.0], "put": [1.0], "delete": [1.0]}, seconds=1, errors=1)
    assert not errored.passed()


# Checks: the saved CSV has a header and one row per operation, including empty ones (requirement A5 "saved").
# Setup: a report with 2 gets, 1 put and 0 deletes.
# Action: render it as CSV.
# Expected: the fixed header line, then rows starting "get,2,", "put,1,", "delete,0,".
def test_report_csv_has_one_row_per_op():
    rep = Report(latencies={"get": [1.0, 2.0], "put": [3.0], "delete": []}, seconds=1, errors=0)
    lines = rep.to_csv().strip().splitlines()
    assert lines[0] == "op,count,ops_per_sec,p50_ms,p95_ms,p99_ms,p999_ms,max_ms,hits,not_found"
    assert lines[1].startswith("get,2,")
    assert lines[2].startswith("put,1,")
    assert lines[3].startswith("delete,0,")


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "kv.db"))
    s.load()
    s.start()
    yield s
    s.close()


# Checks: the open-loop generator issues about rate x seconds requests in the 80/15/5 mix (requirement A5).
# Setup: a real single-server test app; 200 ops/s for 1 s over 100 keys with 64 B values, seed 1.
# Action: run the generator.
# Expected: 150-220 completed ops, no errors, and more gets than puts than deletes.
async def test_run_against_live_server_counts_ops(storage):
    srv = TestServer(create_app(storage, server_id=0, n_servers=1))
    await srv.start_server()
    try:
        cfg = ClusterConfig(servers=(ServerConfig(0, srv.host, srv.port, "d"),))
        rep = await run(cfg, rate=200, seconds=1, n_keys=100, value_size=64, conns_per_server=4, seed=1)
    finally:
        await srv.close()
    total = sum(len(v) for v in rep.latencies.values())
    assert 150 <= total <= 220
    assert rep.errors == 0
    assert len(rep.latencies["get"]) > len(rep.latencies["put"]) > len(rep.latencies["delete"]) >= 0


class StallingClient:
    """Fake KVClient: the first request blocks the whole event loop for 200 ms
    (time.sleep, not asyncio.sleep), as a GC pause or a slow generator would."""

    def __init__(self, cfg, conns_per_server=32):
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def _op(self, *args):
        self.calls += 1
        if self.calls == 1:
            time.sleep(0.2)

    get = put = delete = _op


# Checks: latency is measured from each request's SCHEDULED time, so time a request spends
#   waiting because the generator itself fell behind is counted (no coordinated omission,
#   requirement A5 open loop).
# Setup: a fake client whose first request freezes the event loop for 200 ms; 100 ops/s for 0.5 s.
#   Prefill is off so that first request is a timed one, not a prefill Put.
# Action: run the generator.
# Expected: the ~19 requests scheduled during the freeze are issued late, and each records at
#   least 100 ms of latency; at least 10 such samples overall.
async def test_latency_counts_from_scheduled_time(monkeypatch):
    monkeypatch.setattr(bench, "KVClient", StallingClient)
    cfg = ClusterConfig(servers=(ServerConfig(0, "h", 1, "d"),))
    rep = await run(cfg, rate=100, seconds=0.5, n_keys=10, value_size=8, conns_per_server=4, seed=1,
                    prefill=False)
    all_ms = [ms for v in rep.latencies.values() for ms in v]
    assert sum(1 for ms in all_ms if ms >= 100.0) >= 10


# Checks: errors are classified by type so a failed run says why (421, timeout, connection, other).
# Setup: one exception of each kind the client can raise.
# Action: classify each.
# Expected: WrongOwner -> wrong_owner; timeouts (including aiohttp's ServerTimeoutError, which is
#   also a connection error) -> timeout; other connection errors -> connection; the rest -> other.
def test_classify_error():
    assert classify_error(WrongOwner(421, "WRONG_OWNER 1")) == "wrong_owner"
    assert classify_error(asyncio.TimeoutError()) == "timeout"
    assert classify_error(aiohttp.ServerTimeoutError()) == "timeout"
    assert classify_error(aiohttp.ClientConnectionError()) == "connection"
    assert classify_error(ServerError(500, "commit failed")) == "other"
    assert classify_error(ValueError("x")) == "other"


# Checks: the text report shows the error breakdown, and a hint when any 421 was seen.
# Setup: a report with 3 wrong-owner errors and 1 timeout; and one with only a timeout.
# Action: render each as text.
# Expected: the first lists both counts and the cluster-file hint; the second has no hint.
def test_report_text_shows_error_breakdown_and_421_hint():
    rep = Report(latencies={"get": [1.0]}, seconds=1, errors=4,
                 error_counts={"wrong_owner": 3, "timeout": 1})
    text = rep.to_text()
    assert "wrong_owner=3" in text and "timeout=1" in text
    assert "cluster file" in text
    no_421 = Report(latencies={"get": [1.0]}, seconds=1, errors=1, error_counts={"timeout": 1})
    assert "cluster file" not in no_421.to_text()


# Checks: a client whose cluster file disagrees with the server gets 421s, counted as wrong_owner.
# Setup: a test server that is server 0 of 2; a client config listing 2 servers, both at it.
# Action: run the generator for 1 s at 100 ops/s.
# Expected: some wrong_owner errors, error total equals the sum of the breakdown, run fails.
async def test_run_counts_wrong_owner_errors(storage):
    srv = TestServer(create_app(storage, server_id=0, n_servers=2))
    await srv.start_server()
    try:
        cfg = ClusterConfig(servers=tuple(ServerConfig(i, srv.host, srv.port, "d") for i in range(2)))
        rep = await run(cfg, rate=100, seconds=1, n_keys=100, value_size=8, conns_per_server=2, seed=1)
    finally:
        await srv.close()
    assert rep.error_counts.get("wrong_owner", 0) > 0
    assert rep.errors == sum(rep.error_counts.values())
    assert not rep.passed()


class RecordingClient:
    """Fake KVClient that records every call in order; Get reports every key as present."""

    def __init__(self, cfg, conns_per_server=32):
        self.calls: list[tuple[str, bytes]] = []
        RecordingClient.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def put(self, key, value):
        self.calls.append(("put", key))

    async def get(self, key):
        self.calls.append(("get", key))
        return b"v"

    async def delete(self, key):
        self.calls.append(("delete", key))


# Checks: every key in the key space is written before the timed run, so Gets read real data
#   (realistic workload), and prefill requests are not counted in the measured latencies.
# Setup: a recording fake client; 50 keys; 100 ops/s for 0.2 s.
# Action: run the generator with prefill on (the default).
# Expected: the first 50 calls are Puts covering all 50 keys exactly once; the report says 50 keys
#   were prefilled; the measured op count is only the ~20 timed requests.
async def test_prefill_puts_every_key_before_timed_run(monkeypatch):
    monkeypatch.setattr(bench, "KVClient", RecordingClient)
    cfg = ClusterConfig(servers=(ServerConfig(0, "h", 1, "d"),))
    rep = await run(cfg, rate=100, seconds=0.2, n_keys=50, value_size=8, conns_per_server=4, seed=1)
    calls = RecordingClient.last.calls
    first = calls[:50]
    assert all(op == "put" for op, _ in first)
    assert sorted(k for _, k in first) == sorted(f"key-{i:08d}".encode() for i in range(50))
    assert rep.prefill_keys == 50
    timed = sum(len(v) for v in rep.latencies.values())
    assert timed == len(calls) - 50
    assert 15 <= timed <= 25


# Checks: with prefill off, nothing is written before the timed run (escape hatch for runs on
#   data that is already loaded).
# Setup: a recording fake client; 50 keys; 100 ops/s for 0.2 s.
# Action: run the generator with prefill=False.
# Expected: no extra calls beyond the timed ones, and prefill_keys is 0.
async def test_no_prefill_skips_writes(monkeypatch):
    monkeypatch.setattr(bench, "KVClient", RecordingClient)
    cfg = ClusterConfig(servers=(ServerConfig(0, "h", 1, "d"),))
    rep = await run(cfg, rate=100, seconds=0.2, n_keys=50, value_size=8, conns_per_server=4, seed=1,
                    prefill=False)
    assert rep.prefill_keys == 0
    assert sum(len(v) for v in rep.latencies.values()) == len(RecordingClient.last.calls)


# Checks: the report shows the Get hit / not-found split in text and CSV.
# Setup: a report with 90 Gets, of which 81 hits and 9 not found.
# Action: render text and CSV.
# Expected: text shows "hits 81 (90.0%)" and "not found 9 (10.0%)"; the CSV get row ends with
#   "81,9" and the put/delete rows leave both columns empty.
def test_report_shows_get_hit_ratio():
    rep = Report(latencies={"get": [1.0] * 90, "put": [1.0], "delete": [1.0]}, seconds=1, errors=0,
                 get_hits=81, get_not_found=9)
    text = rep.to_text()
    assert "hits 81 (90.0%)" in text and "not found 9 (10.0%)" in text
    lines = rep.to_csv().strip().splitlines()
    assert lines[1].startswith("get,") and lines[1].endswith(",81,9")
    assert lines[2].endswith(",,") and lines[3].endswith(",,")


# Checks: after prefill, Gets mostly find their key, so the read path measured is a real lookup
#   (the bug: without prefill nearly every Get was a NotFound).
# Setup: a real single-server test app; 100 keys; 200 ops/s for 1 s.
# Action: run the generator with prefill.
# Expected: hits + not found equals the number of Gets, and the hit ratio is above 90%. Only the
#   ~5% Deletes can make a key absent, and Puts recreate keys.
async def test_get_hit_ratio_above_90_percent_after_prefill(storage):
    srv = TestServer(create_app(storage, server_id=0, n_servers=1))
    await srv.start_server()
    try:
        cfg = ClusterConfig(servers=(ServerConfig(0, srv.host, srv.port, "d"),))
        rep = await run(cfg, rate=200, seconds=1, n_keys=100, value_size=64, conns_per_server=4, seed=1)
    finally:
        await srv.close()
    assert rep.errors == 0
    assert rep.get_hits + rep.get_not_found == len(rep.latencies["get"])
    assert rep.get_hits / len(rep.latencies["get"]) > 0.90


# Checks: the per-second table buckets timed requests by their scheduled second, so a slow
#   period (e.g. a checkpoint stall) is visible instead of being averaged into the run.
# Setup: a report with a hand-built timeline: second 0 has 2 puts, 1 delete, 2 gets and a failed
#   put; second 1 has 1 put; the run started at wall time 1000.0.
# Action: compute per-second rows.
# Expected: 2 rows. Second 0: writes=3 (puts + delete, successes only), w_p50=20, w_p99=30,
#   w_max=30, errors=1, g_p99=2, epoch=1000. Second 1: writes=1, no gets (g_p99 None), epoch=1001.
def test_per_second_rows_bucket_by_scheduled_second():
    timeline = [(0.1, "put", 10.0), (0.5, "put", 30.0), (0.9, "delete", 20.0),
                (0.2, "get", 1.0), (0.3, "get", 2.0), (0.4, "put", None), (1.2, "put", 5.0)]
    rep = Report(latencies={"get": [1.0, 2.0], "put": [10.0, 30.0, 5.0], "delete": [20.0]},
                 seconds=2, errors=1, timeline=timeline, started_at=1000.0)
    rows = rep.per_second()
    assert [r["t"] for r in rows] == [0, 1]
    r0, r1 = rows
    assert (r0["writes"], r0["w_p50"], r0["w_p99"], r0["w_max"], r0["errors"], r0["g_p99"]) == (3, 20.0, 30.0, 30.0, 1, 2.0)
    assert r0["epoch"] == 1000.0
    assert (r1["writes"], r1["w_p99"], r1["g_p99"], r1["epoch"]) == (1, 5.0, None, 1001.0)


# Checks: the per-second table renders as text and CSV with a fixed header.
# Setup: the same kind of report with one second of data.
# Action: render text and CSV.
# Expected: text header names the columns and has one data row; CSV header is
#   "t,epoch,writes,w_p50_ms,w_p99_ms,w_max_ms,errors,g_p99_ms" and one row starting "0,1000.000,1,".
def test_per_second_text_and_csv():
    rep = Report(latencies={"get": [], "put": [4.0], "delete": []}, seconds=1, errors=0,
                 timeline=[(0.5, "put", 4.0)], started_at=1000.0)
    text = rep.per_second_text().splitlines()
    assert text[0].split() == ["t", "writes", "w_p50", "w_p99", "w_max", "errs", "g_p99"]
    assert len(text) == 2
    csv_lines = rep.per_second_csv().strip().splitlines()
    assert csv_lines[0] == "t,epoch,writes,w_p50_ms,w_p99_ms,w_max_ms,errors,g_p99_ms"
    assert csv_lines[1].startswith("0,1000.000,1,")


# Checks: run() records one timeline entry per timed request, stamped relative to the run start,
#   and records the wall-clock start (needed to line seconds up with server logs).
# Setup: a recording fake client; prefill off; 100 ops/s for 0.5 s.
# Action: run the generator.
# Expected: timeline length equals the number of measured requests, every t is in [0, 0.5), and
#   started_at is a wall-clock time close to now.
async def test_run_records_timeline(monkeypatch):
    monkeypatch.setattr(bench, "KVClient", RecordingClient)
    cfg = ClusterConfig(servers=(ServerConfig(0, "h", 1, "d"),))
    before = time.time()
    rep = await run(cfg, rate=100, seconds=0.5, n_keys=10, value_size=8, conns_per_server=4, seed=1,
                    prefill=False)
    assert len(rep.timeline) == sum(len(v) for v in rep.latencies.values())
    assert all(0.0 <= t < 0.5 for t, _, _ in rep.timeline)
    assert before - 1 <= rep.started_at <= time.time()


# Checks: the CLI --per-second flag prints the table and, with --out, saves it as a second CSV.
# Setup: a recording fake client; a one-server cluster file; 50 ops/s for 0.2 s, prefill off.
# Action: call main() with --per-second --out <tmp>/run.csv.
# Expected: stdout contains the per-second header; <tmp>/run.per-second.csv exists with its header.
def test_cli_per_second_prints_and_saves(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bench, "KVClient", RecordingClient)
    cfg = tmp_path / "c.json"
    cfg.write_text('{"servers": [{"id": 0, "host": "h", "port": 1, "data_dir": "d"}]}')
    out = tmp_path / "run.csv"
    bench.main(["--config", str(cfg), "--rate", "50", "--seconds", "0.2", "--keys", "5",
                "--no-prefill", "--per-second", "--out", str(out)])
    assert "w_p99" in capsys.readouterr().out
    ps = tmp_path / "run.per-second.csv"
    assert ps.read_text().startswith("t,epoch,writes,")
