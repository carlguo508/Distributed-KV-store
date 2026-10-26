import pytest
from aiohttp.test_utils import TestServer

from kvstore.bench import READ_P99_MS, WRITE_P99_MS, Report, percentile, pick_op, run
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
    assert lines[0] == "op,count,ops_per_sec,p50_ms,p95_ms,p99_ms,p999_ms,max_ms"
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
