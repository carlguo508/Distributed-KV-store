import asyncio
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


def start_server(cfg_path: Path, server_id: int) -> subprocess.Popen:
    """Run a real server process and wait until its port accepts connections.
    The port opens only after the committed state is loaded (design.md §7 step 4)."""
    proc = subprocess.Popen([sys.executable, "-m", "kvstore.server", "--config", str(cfg_path), "--id", str(server_id)],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = load_config(cfg_path)
    s = cfg.servers[server_id]
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection((s.host, s.port), 0.2).close()
            return proc
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start")


# Checks: acknowledged writes survive a crash (requirement A8 and §2 Restart recovery; design.md §4
#   "Acked write, then restart", §7 SIGKILL row).
# Setup: one real server process on a fresh data dir; 8 concurrent writers, each cycling Puts and
#   occasional Deletes over its own 20 keys. Each writer records an op as *pending* before sending
#   it and moves it to *acked* when the 200 arrives, and stops at its first error.
# Action: after 1 s of load, SIGKILL the server; restart it on the same data dir; Get every key.
# Expected: each key holds its last acked value (NotFound for an acked Delete or never-acked key),
#   OR the value of its one pending op. A pending op may have committed before the kill even
#   though its 200 was never sent, which is correct. Any other value is a durability bug.
async def test_acked_writes_survive_sigkill(tmp_path):
    cfg_path = tmp_path / "cluster.json"
    cfg_path.write_text(json.dumps({"servers": [
        {"id": 0, "host": "127.0.0.1", "port": free_port(), "data_dir": str(tmp_path / "s0")}]}))
    cfg = load_config(cfg_path)
    proc = start_server(cfg_path, 0)

    acked: dict[bytes, object] = {}               # key -> last acked value; None = acked delete
    pending: dict[bytes, object] = {}             # key -> value of the one un-acked op; None = delete
    stop = asyncio.Event()

    async def writer(idx: int, kv: KVClient) -> None:
        # Disjoint keys per writer, so each key has one writer and a clear "last acked" value.
        keys = [f"w{idx}-k{i}".encode() for i in range(20)]
        n = 0
        while not stop.is_set():
            key = keys[n % len(keys)]
            value = None if n % 7 == 6 else f"w{idx}-v{n}".encode()
            pending[key] = value                  # recorded before the request leaves
            try:
                if value is None:
                    await kv.delete(key)
                else:
                    await kv.put(key, value)
            except Exception:  # noqa: BLE001 - killed mid-request: outcome unknown, stays pending
                return                            # stop at first error: at most one pending op per key
            acked[key] = value                    # response arrived: promote
            del pending[key]
            n += 1

    async with KVClient(cfg, timeout=2.0) as kv:
        tasks = [asyncio.create_task(writer(i, kv)) for i in range(8)]
        await asyncio.sleep(1.0)                      # load is flowing
        os.kill(proc.pid, signal.SIGKILL)             # crash mid-load
        proc.wait(timeout=10)
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.gather(*tasks)

    assert len(acked) > 50                            # the kill really happened under load
    assert len(pending) <= 8                          # one un-acked op per writer at most
    data_dir = Path(cfg.servers[0].data_dir)
    assert (data_dir / "kv.db").exists()

    proc = start_server(cfg_path, 0)                  # restart on the same data dir
    try:
        async with KVClient(cfg) as kv:
            for key in set(acked) | set(pending):
                got = await kv.get(key)               # None means NotFound
                valid = {acked[key]} if key in acked else {None}   # never acked: absent is valid
                if key in pending:
                    valid.add(pending[key])           # committed but 200 never sent; None = delete
                assert got in valid, f"key {key!r}: got {got!r}, valid {valid!r}"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
