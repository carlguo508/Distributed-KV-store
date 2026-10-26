import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kvstore.client import KVClient
from kvstore.config import load_config

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.integration


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_cluster(tmp_path, n) -> Path:
    cfg = {"servers": [
        {"id": i, "host": "127.0.0.1", "port": free_port(), "data_dir": str(tmp_path / f"s{i}")}
        for i in range(n)], "max_batch": 256}
    p = tmp_path / "cluster.json"
    p.write_text(json.dumps(cfg))
    return p


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# Checks: start.sh brings up every server in a config and stop.sh takes them down without
#   deleting data (requirement F10, design.md §6 Scripts, §8 Integration).
# Setup: a 3-server config on free ports with data dirs under tmp_path.
# Action: run start.sh; put and get 30 keys through the routing client; run stop.sh.
# Expected: start exits 0 with a live pid per server; every key reads back; stop exits 0;
#   every pid exits within 10 s; each kv.db still exists and each pid file is removed.
async def test_start_and_stop_three_servers(tmp_path):
    cfg_path = write_cluster(tmp_path, 3)
    env = {**os.environ, "PYTHON": sys.executable}
    out = subprocess.run(["scripts/start.sh", str(cfg_path)], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    cfg = load_config(cfg_path)
    pids = [int((Path(s.data_dir) / "kvserver.pid").read_text()) for s in cfg.servers]
    assert all(pid_alive(p) for p in pids)
    try:
        async with KVClient(cfg) as kv:
            for i in range(30):
                await kv.put(f"k{i}".encode(), f"v{i}".encode())
            for i in range(30):
                assert await kv.get(f"k{i}".encode()) == f"v{i}".encode()
    finally:
        out = subprocess.run(["scripts/stop.sh", str(cfg_path)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    deadline = time.time() + 10
    while time.time() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.1)
    assert not any(pid_alive(p) for p in pids)
    for s in cfg.servers:
        assert (Path(s.data_dir) / "kv.db").exists()          # stop never deletes data
        assert not (Path(s.data_dir) / "kvserver.pid").exists()


# Checks: start.sh fails loudly when a server never becomes ready, instead of reporting success
#   (requirement F10, design.md §6 "waits until each port accepts a TCP connection").
# Setup: a 1-server config whose data dir contains a DIRECTORY named kv.db, so SQLite cannot
#   open the database and the server exits before listening. START_TIMEOUT=2 keeps the test short.
# Action: run start.sh.
# Expected: non-zero exit; stderr names server 0 and its log file; no "listening" line on stdout;
#   no pid file left behind.
def test_start_fails_when_port_never_ready(tmp_path):
    cfg_path = write_cluster(tmp_path, 1)
    data_dir = Path(load_config(cfg_path).servers[0].data_dir)
    (data_dir / "kv.db").mkdir(parents=True)
    env = {**os.environ, "PYTHON": sys.executable, "START_TIMEOUT": "2"}
    out = subprocess.run(["scripts/start.sh", str(cfg_path)], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode != 0
    assert "server 0" in out.stderr
    assert str(data_dir / "kvserver.log") in out.stderr
    assert "listening" not in out.stdout
    assert not (data_dir / "kvserver.pid").exists()


# Checks: start.sh refuses a port that something else already listens on, names the holder
#   and says how to fix it, instead of mistaking the holder for its own server (the
#   2026-10-26 run where an orphan n=1 server answered for s0).
# Setup: a 1-server config whose port this test process holds with a listening socket.
# Action: run start.sh.
# Expected: non-zero exit; stderr says "already in use", names this test's pid and has a
#   "fix:" line; no server was launched, so no pid file and no log.
def test_start_refuses_port_in_use(tmp_path):
    cfg_path = write_cluster(tmp_path, 1)
    s = load_config(cfg_path).servers[0]
    holder = socket.socket()
    holder.bind((s.host, s.port))
    holder.listen()
    try:
        env = {**os.environ, "PYTHON": sys.executable, "START_TIMEOUT": "5"}
        out = subprocess.run(["scripts/start.sh", str(cfg_path)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=60)
    finally:
        holder.close()
    assert out.returncode != 0
    assert "already in use" in out.stderr
    assert str(os.getpid()) in out.stderr
    assert "fix:" in out.stderr
    assert not (Path(s.data_dir) / "kvserver.pid").exists()
    assert not (Path(s.data_dir) / "kvserver.log").exists()


# Checks: start.sh notices at once when its server process exits during startup, instead of
#   waiting out START_TIMEOUT.
# Setup: a 1-server config whose data dir has a DIRECTORY named kv.db, so the server exits
#   before listening; START_TIMEOUT=30.
# Action: run start.sh and time it.
# Expected: non-zero exit well under 30 s (< 10 s); stderr says the server exited and shows
#   the tail of its log; no pid file left behind.
def test_start_fails_fast_when_server_exits(tmp_path):
    cfg_path = write_cluster(tmp_path, 1)
    data_dir = Path(load_config(cfg_path).servers[0].data_dir)
    (data_dir / "kv.db").mkdir(parents=True)
    env = {**os.environ, "PYTHON": sys.executable, "START_TIMEOUT": "30"}
    t0 = time.monotonic()
    out = subprocess.run(["scripts/start.sh", str(cfg_path)], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode != 0
    assert time.monotonic() - t0 < 10
    assert "exited" in out.stderr
    assert "Error" in out.stderr                      # the log tail (sqlite3/OSError traceback)
    assert not (data_dir / "kvserver.pid").exists()
