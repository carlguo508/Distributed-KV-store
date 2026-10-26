# KV Store v1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the v1 distributed key-value store specified in [design.md](../design.md) and [requirement.md](../requirement.md): N hash-partitioned Python servers with SQLite durability, an HTTP API, a client library, start/stop scripts and a workload generator that proves p99 targets at 1K ops/s for N=1 and N=3.

**Architecture:** Each server is one asyncio process. aiohttp handlers serve GET from an in-memory dict and push PUT/DELETE records onto a queue. A single writer thread drains the queue, commits one SQLite transaction per batch (WAL, `synchronous=FULL`, `fullfsync=ON`), then applies the batch to the dict and resolves the waiting futures. Clients route by `md5(key) % N` using a shared `cluster.json`.

**Tech Stack:** Python 3.12+, aiohttp, sqlite3 (stdlib), pytest, pytest-aiohttp. No other dependencies.

**Rules for the implementer:**
- Do not edit `docs/design.md` or `docs/requirement.md`. If the code must deviate, stop and report.
- Every task is test-first: write the failing test, run it, see it fail, implement, run it, see it pass, commit.
- Run tests with the project venv: `.venv/bin/pytest`. All commands below assume the repo root as cwd.

---

## Milestones

| # | Milestone | Tasks | Done when |
|---|-----------|-------|-----------|
| M0 | Scaffolding | 1 | venv, package, pytest run green on a trivial test |
| M1 | Config and key codec | 2, 3 | `load_config`, `owner_of`, `encode_key`/`decode_key` tested |
| M2 | Storage | 4 | writer thread batches, applies after COMMIT, fail-stops |
| M3 | HTTP server | 5 | all status codes from design §5 tested with `aiohttp_client` |
| M4 | Client library | 6 | routing, 404 → `None`, 421 → `WrongOwner` tested |
| M5 | Scripts | 7 | `start.sh`/`stop.sh` bring a 3-server cluster up and down |
| M6 | Workload generator | 8 | open-loop generator reports percentiles and pass/fail |
| M7 | Restart test | 9 | SIGKILL mid-load, restart, every acked write verified |
| M8 | Final measurement | 10 | N=1 and N=3 at 1K ops/s for 60 s pass, results in README |

Final layout:

```
kvstore/            package: config.py keycodec.py storage.py server.py client.py bench.py
tests/              pytest
scripts/            start.sh stop.sh
cluster.json        N=3 default
cluster-1.json      N=1
README.md           quickstart + results
requirements.txt
```

---

### Task 1: Scaffolding

**Files:**
- Create: `requirements.txt`, `kvstore/__init__.py`, `tests/__init__.py`, `tests/test_smoke.py`, `pytest.ini`
- Modify: `.gitignore`

**Step 1: Create venv and dependencies**

```bash
python3 -m venv .venv
printf 'aiohttp>=3.9\npytest>=8\npytest-aiohttp>=1.0\n' > requirements.txt
.venv/bin/pip install -q -r requirements.txt
```

**Step 2: Package, pytest config, gitignore**

```bash
mkdir -p kvstore tests
printf '"""Distributed key-value store, v1."""\n' > kvstore/__init__.py
: > tests/__init__.py
printf '[pytest]\nasyncio_mode = auto\ntestpaths = tests\n' > pytest.ini
printf '.venv/\ndata/\nresults/\n*.pid\n*.log\n.pytest_cache/\n' >> .gitignore
```

**Step 3: Write the smoke test**

`tests/test_smoke.py`:
```python
import kvstore


def test_package_imports():
    assert kvstore.__doc__.startswith("Distributed key-value store")
```

**Step 4: Run it**

Run: `.venv/bin/pytest -q`
Expected: `1 passed`

**Step 5: Commit**

```bash
git add requirements.txt pytest.ini kvstore tests .gitignore
git commit -m "chore: scaffold kvstore package and test setup"
```

---

### Task 2: Config loading and key ownership

**Files:**
- Create: `kvstore/config.py`, `tests/test_config.py`, `cluster.json`, `cluster-1.json`

**Step 1: Write the failing tests**

`tests/test_config.py`:
```python
import hashlib
import json

import pytest

from kvstore.config import ClusterConfig, ServerConfig, load_config, owner_of


def write(tmp_path, servers, max_batch=256):
    p = tmp_path / "cluster.json"
    p.write_text(json.dumps({"servers": servers, "max_batch": max_batch}))
    return p


def test_load_config_parses_servers(tmp_path):
    p = write(tmp_path, [
        {"id": 0, "host": "127.0.0.1", "port": 7300, "data_dir": "data/s0"},
        {"id": 1, "host": "127.0.0.1", "port": 7301, "data_dir": "data/s1"},
    ], max_batch=64)
    cfg = load_config(p)
    assert isinstance(cfg, ClusterConfig)
    assert cfg.max_batch == 64
    assert cfg.servers[1] == ServerConfig(id=1, host="127.0.0.1", port=7301, data_dir="data/s1")
    assert len(cfg.servers) == 2


def test_max_batch_defaults_to_256(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"servers": [{"id": 0, "host": "h", "port": 1, "data_dir": "d"}]}))
    assert load_config(p).max_batch == 256


def test_ids_must_match_positions(tmp_path):
    p = write(tmp_path, [{"id": 1, "host": "h", "port": 1, "data_dir": "d"}])
    with pytest.raises(ValueError, match="id"):
        load_config(p)


@pytest.mark.parametrize("n", [0, 5])
def test_server_count_must_be_1_to_4(tmp_path, n):
    servers = [{"id": i, "host": "h", "port": 1 + i, "data_dir": f"d{i}"} for i in range(n)]
    with pytest.raises(ValueError, match="1 and 4"):
        load_config(write(tmp_path, servers))


def test_owner_of_is_md5_mod_n():
    key = b"job-42"
    expected = int(hashlib.md5(key).hexdigest(), 16) % 3
    assert owner_of(key, 3) == expected


def test_owner_of_single_server_is_always_zero():
    for k in (b"", b"a", b"x" * 256):
        assert owner_of(k, 1) == 0


def test_owner_of_covers_all_servers():
    owners = {owner_of(f"k{i}".encode(), 4) for i in range(200)}
    assert owners == {0, 1, 2, 3}
```

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kvstore.config'`

**Step 3: Implement**

`kvstore/config.py`:
```python
"""Cluster configuration shared by servers and clients (design.md §6)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

MIN_SERVERS, MAX_SERVERS = 1, 4
DEFAULT_MAX_BATCH = 256


@dataclass(frozen=True)
class ServerConfig:
    id: int
    host: str
    port: int
    data_dir: str


@dataclass(frozen=True)
class ClusterConfig:
    servers: tuple[ServerConfig, ...]
    max_batch: int = DEFAULT_MAX_BATCH

    @property
    def n(self) -> int:
        return len(self.servers)


def load_config(path: str | Path) -> ClusterConfig:
    raw = json.loads(Path(path).read_text())
    servers = tuple(ServerConfig(**s) for s in raw["servers"])
    if not MIN_SERVERS <= len(servers) <= MAX_SERVERS:
        raise ValueError(f"server count must be between {MIN_SERVERS} and {MAX_SERVERS}, got {len(servers)}")
    for pos, s in enumerate(servers):
        if s.id != pos:
            raise ValueError(f"server at position {pos} has id {s.id}; ids must equal list positions")
    return ClusterConfig(servers=servers, max_batch=int(raw.get("max_batch", DEFAULT_MAX_BATCH)))


def owner_of(key: bytes, n: int) -> int:
    """Index of the server that owns `key`: md5(key) mod n."""
    return int(hashlib.md5(key).hexdigest(), 16) % n
```

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: `8 passed`

**Step 5: Create the two cluster files**

`cluster.json`:
```json
{
  "servers": [
    {"id": 0, "host": "127.0.0.1", "port": 7300, "data_dir": "data/s0"},
    {"id": 1, "host": "127.0.0.1", "port": 7301, "data_dir": "data/s1"},
    {"id": 2, "host": "127.0.0.1", "port": 7302, "data_dir": "data/s2"}
  ],
  "max_batch": 256
}
```

`cluster-1.json`:
```json
{
  "servers": [
    {"id": 0, "host": "127.0.0.1", "port": 7300, "data_dir": "data/single"}
  ],
  "max_batch": 256
}
```

Check: `.venv/bin/python -c "from kvstore.config import load_config; print(load_config('cluster.json').n, load_config('cluster-1.json').n)"` prints `3 1`.

**Step 6: Commit**

```bash
git add kvstore/config.py tests/test_config.py cluster.json cluster-1.json
git commit -m "feat: cluster config loading and md5 key ownership"
```

---

### Task 3: Key codec and limits

**Files:**
- Create: `kvstore/keycodec.py`, `tests/test_keycodec.py`

**Step 1: Write the failing tests**

`tests/test_keycodec.py`:
```python
import pytest

from kvstore.keycodec import MAX_KEY, MAX_VALUE, decode_key, encode_key


def test_limits_match_requirements():
    assert MAX_KEY == 256
    assert MAX_VALUE == 64 * 1024


def test_encode_is_base64url_without_padding():
    assert encode_key(b"job-1") == "am9iLTE"
    assert "=" not in encode_key(b"\xff\xfe\xfd\xfc\xfb")
    assert "+" not in encode_key(bytes(range(256))) and "/" not in encode_key(bytes(range(256)))


@pytest.mark.parametrize("key", [b"a", b"job-1", b"\x00\xff", bytes(range(256)), b"x" * 256])
def test_roundtrip(key):
    assert decode_key(encode_key(key)) == key


def test_decode_accepts_padded_input():
    assert decode_key("am9iLTE=") == b"job-1"


def test_decode_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        decode_key("")


def test_decode_rejects_non_base64url_characters():
    with pytest.raises(ValueError):
        decode_key("am9i+LTE")
    with pytest.raises(ValueError):
        decode_key("am9i/LTE")
    with pytest.raises(ValueError):
        decode_key("am9i LTE")


def test_decode_rejects_invalid_length():
    with pytest.raises(ValueError):
        decode_key("a")  # one base64 char can never form a byte


def test_decode_rejects_key_over_256_bytes():
    with pytest.raises(ValueError, match="256"):
        decode_key(encode_key(b"x" * 257))
```

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_keycodec.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kvstore.keycodec'`

**Step 3: Implement**

`kvstore/keycodec.py`:
```python
"""Key encoding for the URL path (design.md §5) and size limits (requirement F5)."""
from __future__ import annotations

import base64
import binascii
import re

MAX_KEY = 256
MAX_VALUE = 64 * 1024

_B64URL = re.compile(r"^[A-Za-z0-9_-]+=*$")


def encode_key(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")


def decode_key(text: str) -> bytes:
    if not text:
        raise ValueError("empty key")
    if not _B64URL.match(text):
        raise ValueError("key is not base64url")
    stripped = text.rstrip("=")
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        key = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"key is not valid base64url: {exc}") from exc
    if not key:
        raise ValueError("empty key")
    if len(key) > MAX_KEY:
        raise ValueError(f"key longer than {MAX_KEY} bytes")
    return key
```

Note: `base64.urlsafe_b64decode("a===")` raises `binascii.Error` for a 1-char group; the `except` turns it into `ValueError`.

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_keycodec.py -q`
Expected: `12 passed`

**Step 5: Commit**

```bash
git add kvstore/keycodec.py tests/test_keycodec.py
git commit -m "feat: base64url key codec with size limits"
```

---

### Task 4: Storage with batched commits and apply-after-commit

**Files:**
- Create: `kvstore/storage.py`, `tests/test_storage.py`

Behaviour under test (design §2, §4, §7):
- Pragmas set and asserted: `synchronous=2`, `fullfsync=1`, WAL.
- `load()` fills the dict from the committed table.
- `put`/`delete` return a `concurrent.futures.Future` resolved only after COMMIT.
- The dict is not updated before COMMIT returns (apply-after-commit).
- Many concurrent submits are committed in few transactions (batching) in FIFO order.
- On COMMIT failure: every future in the batch gets the exception, nothing is applied, `on_fatal` is called, the writer stops.

**Step 1: Write the failing tests**

`tests/test_storage.py`:
```python
import sqlite3
import threading
import time
from concurrent.futures import Future, wait

import pytest

from kvstore.storage import Storage, connect


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "kv.db")


@pytest.fixture
def storage(db_path):
    s = Storage(db_path)
    s.load()
    s.start()
    yield s
    s.close()


def test_connect_sets_durable_pragmas(db_path):
    conn = connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1
    assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
    conn.close()


def test_put_returns_future_and_is_visible_after_result(storage):
    fut = storage.put(b"k", b"v")
    assert isinstance(fut, Future)
    fut.result(timeout=5)
    assert storage.data[b"k"] == b"v"


def test_empty_value_is_stored_and_distinct_from_missing(storage):
    storage.put(b"k", b"").result(timeout=5)
    assert storage.data[b"k"] == b""
    assert b"other" not in storage.data


def test_delete_removes_and_deleting_absent_succeeds(storage):
    storage.put(b"k", b"v").result(timeout=5)
    storage.delete(b"k").result(timeout=5)
    assert b"k" not in storage.data
    storage.delete(b"k").result(timeout=5)  # absent: still succeeds


def test_committed_data_survives_reopen(db_path):
    s = Storage(db_path)
    s.load()
    s.start()
    s.put(b"a", b"1").result(timeout=5)
    s.put(b"b", b"2").result(timeout=5)
    s.delete(b"a").result(timeout=5)
    s.close()

    s2 = Storage(db_path)
    s2.load()
    assert s2.data == {b"b": b"2"}


def test_fifo_order_for_same_key(storage):
    futs = [storage.put(b"k", str(i).encode()) for i in range(50)]
    futs.append(storage.delete(b"k"))
    futs.append(storage.put(b"k", b"final"))
    wait(futs, timeout=5)
    assert storage.data[b"k"] == b"final"


class BlockingCommit:
    """Wraps a connection; COMMIT blocks until `release` is set."""

    def __init__(self, conn, release: threading.Event, reached: threading.Event):
        self._conn, self._release, self._reached = conn, release, reached

    def execute(self, sql, params=()):
        if sql == "COMMIT":
            self._reached.set()
            assert self._release.wait(timeout=5)
        return self._conn.execute(sql, params)

    def close(self):
        self._conn.close()


def test_value_not_visible_before_commit(db_path):
    release, reached = threading.Event(), threading.Event()
    s = Storage(db_path, connect=lambda p: BlockingCommit(connect(p), release, reached))
    s.load()
    s.start()
    fut = s.put(b"k", b"v")
    assert reached.wait(timeout=5)
    time.sleep(0.05)
    assert b"k" not in s.data           # apply-after-commit
    assert not fut.done()
    release.set()
    fut.result(timeout=5)
    assert s.data[b"k"] == b"v"
    s.close()


class CountingCommit:
    def __init__(self, conn, counter):
        self._conn, self._counter = conn, counter

    def execute(self, sql, params=()):
        if sql == "COMMIT":
            self._counter.append(1)
        return self._conn.execute(sql, params)

    def close(self):
        self._conn.close()


def test_concurrent_submits_are_batched(db_path):
    commits = []
    release, reached = threading.Event(), threading.Event()
    # First commit blocks so the queue fills; everything queued must land in few commits.
    class Gate(CountingCommit):
        def execute(self, sql, params=()):
            if sql == "COMMIT" and not commits:
                reached.set()
                assert release.wait(timeout=5)
            return super().execute(sql, params)
    s = Storage(db_path, max_batch=256, connect=lambda p: Gate(connect(p), commits))
    s.load()
    s.start()
    first = s.put(b"k0", b"v")
    assert reached.wait(timeout=5)
    futs = [s.put(f"k{i}".encode(), b"v") for i in range(1, 101)]
    release.set()
    wait([first, *futs], timeout=5)
    assert len(commits) <= 3            # 1 for the gated write, 1-2 for the 100 queued writes
    assert len(s.data) == 101
    s.close()


class FailingCommit:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        if sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, params)

    def close(self):
        self._conn.close()


def test_commit_failure_fails_batch_applies_nothing_and_calls_on_fatal(db_path):
    fatal = []
    s = Storage(db_path, connect=lambda p: FailingCommit(connect(p)), on_fatal=fatal.append)
    s.load()
    s.start()
    f1, f2 = s.put(b"a", b"1"), s.put(b"b", b"2")
    with pytest.raises(sqlite3.OperationalError):
        f1.result(timeout=5)
    with pytest.raises(sqlite3.OperationalError):
        f2.result(timeout=5)
    assert s.data == {}
    assert len(fatal) == 1 and isinstance(fatal[0], sqlite3.OperationalError)
    s.close()
```

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_storage.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kvstore.storage'`

**Step 3: Implement**

`kvstore/storage.py`:
```python
"""SQLite-backed storage with a single writer thread, batched commits and
apply-after-commit (design.md §2, §4, §7)."""
from __future__ import annotations

import os
import queue
import sqlite3
import sys
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable

PUT = "put"
DELETE = "delete"

_PRAGMAS = ("journal_mode=WAL", "synchronous=FULL", "fullfsync=ON", "checkpoint_fullfsync=ON")
_SCHEMA = "CREATE TABLE IF NOT EXISTS kv(k BLOB PRIMARY KEY, v BLOB NOT NULL) WITHOUT ROWID"


def connect(path: str):
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    for pragma in _PRAGMAS:
        conn.execute(f"PRAGMA {pragma}")
    conn.execute(_SCHEMA)
    assert_durable(conn)
    return conn


def assert_durable(conn) -> None:
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    fullfsync = conn.execute("PRAGMA fullfsync").fetchone()[0]
    if synchronous != 2 or fullfsync != 1:
        raise RuntimeError(f"durability pragmas not in effect: synchronous={synchronous} fullfsync={fullfsync}")


def _default_on_fatal(exc: BaseException) -> None:
    print(f"kvstore: commit failed, exiting: {exc!r}", file=sys.stderr, flush=True)
    os._exit(1)


@dataclass
class _Record:
    op: str
    key: bytes
    value: bytes
    future: Future


class Storage:
    """Owns the committed-state dict and the writer thread.

    Invariant: `data` always equals the committed content of the database.
    Only `load()` (before `start()`) and the writer thread ever mutate it.
    """

    def __init__(self, path: str, max_batch: int = 256,
                 connect: Callable[[str], object] = connect,
                 on_fatal: Callable[[BaseException], None] = _default_on_fatal):
        self.path = path
        self.max_batch = max_batch
        self._connect = connect
        self._on_fatal = on_fatal
        self.data: dict[bytes, bytes] = {}
        self._queue: queue.Queue = queue.Queue()
        self._conn = None
        self._thread: threading.Thread | None = None

    # ---- lifecycle -------------------------------------------------------
    def load(self) -> None:
        """Open the database (replays the WAL) and load every committed row."""
        conn = self._connect(self.path)
        try:
            self.data = {k: v for k, v in conn.execute("SELECT k, v FROM kv")}
        finally:
            conn.close()

    def start(self) -> None:
        self._conn = self._connect(self.path)
        self._thread = threading.Thread(target=self._run, name="kv-writer", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=10)
            self._thread = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- public write API ------------------------------------------------
    def put(self, key: bytes, value: bytes) -> Future:
        return self._submit(PUT, key, value)

    def delete(self, key: bytes) -> Future:
        return self._submit(DELETE, key, b"")

    def _submit(self, op: str, key: bytes, value: bytes) -> Future:
        fut: Future = Future()
        self._queue.put(_Record(op, key, value, fut))
        return fut

    # ---- writer thread ---------------------------------------------------
    def _run(self) -> None:
        while True:
            first = self._queue.get()
            if first is None:
                return
            batch = [first]
            stop = False
            while len(batch) < self.max_batch:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    stop = True
                    break
                batch.append(item)
            if not self._commit(batch) or stop:
                return

    def _commit(self, batch: list[_Record]) -> bool:
        conn = self._conn
        try:
            conn.execute("BEGIN")
            for rec in batch:
                if rec.op == PUT:
                    conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (rec.key, rec.value))
                else:
                    conn.execute("DELETE FROM kv WHERE k = ?", (rec.key,))
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - any failure here is fatal by design
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            for rec in batch:
                rec.future.set_exception(exc)
            self._on_fatal(exc)
            return False
        for rec in batch:                      # durable now: apply in queue order
            if rec.op == PUT:
                self.data[rec.key] = rec.value
            else:
                self.data.pop(rec.key, None)
        for rec in batch:
            rec.future.set_result(None)
        return True
```

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_storage.py -q`
Expected: `9 passed`

If `test_connect_sets_durable_pragmas` fails on `fullfsync` on Linux: the pragma is a per-connection flag and still reads back `1`; if it does not on your build, stop and report rather than loosening the assert.

**Step 5: Commit**

```bash
git add kvstore/storage.py tests/test_storage.py
git commit -m "feat: SQLite storage with writer thread, batched commits, apply-after-commit"
```

---

### Task 5: HTTP server

**Files:**
- Create: `kvstore/server.py`, `tests/test_server.py`

**Step 1: Write the failing tests**

`tests/test_server.py`:
```python
import pytest

from kvstore.config import owner_of
from kvstore.keycodec import MAX_VALUE, encode_key
from kvstore.server import create_app
from kvstore.storage import Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "kv.db"))
    s.load()
    s.start()
    yield s
    s.close()


@pytest.fixture
async def client(aiohttp_client, storage):
    return await aiohttp_client(create_app(storage, server_id=0, n_servers=1))


def url(key: bytes) -> str:
    return f"/keys/{encode_key(key)}"


async def test_get_missing_is_404_notfound(client):
    r = await client.get(url(b"nope"))
    assert r.status == 404
    assert await r.text() == "NotFound"


async def test_put_then_get_returns_value(client):
    r = await client.put(url(b"job-1"), data=b"running")
    assert r.status == 200
    assert await r.read() == b""
    r = await client.get(url(b"job-1"))
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/octet-stream"
    assert await r.read() == b"running"


async def test_put_replaces_whole_value(client):
    await client.put(url(b"k"), data=b"first-long-value")
    await client.put(url(b"k"), data=b"2")
    r = await client.get(url(b"k"))
    assert await r.read() == b"2"


async def test_empty_value_is_200_with_empty_body(client):
    r = await client.put(url(b"k"), data=b"")
    assert r.status == 200
    r = await client.get(url(b"k"))
    assert r.status == 200
    assert await r.read() == b""


async def test_delete_then_get_is_404_and_delete_absent_is_200(client):
    await client.put(url(b"k"), data=b"v")
    r = await client.delete(url(b"k"))
    assert r.status == 200
    r = await client.get(url(b"k"))
    assert r.status == 404
    r = await client.delete(url(b"k"))
    assert r.status == 200


async def test_binary_key_and_value_roundtrip(client):
    key, value = bytes(range(256)), bytes(range(256)) * 4
    assert (await client.put(url(key), data=value)).status == 200
    assert await (await client.get(url(key))).read() == value


async def test_value_at_limit_ok_and_over_limit_413(client):
    assert (await client.put(url(b"k"), data=b"x" * MAX_VALUE)).status == 200
    r = await client.put(url(b"k"), data=b"x" * (MAX_VALUE + 1))
    assert r.status == 413


async def test_bad_key_encoding_is_400(client):
    r = await client.get("/keys/not+base64url")
    assert r.status == 400


async def test_key_over_256_bytes_is_400(client):
    r = await client.put(url(b"x" * 257), data=b"v")
    assert r.status == 400


async def test_empty_key_path_is_400(client):
    r = await client.get("/keys/")
    assert r.status == 400


async def test_wrong_owner_is_421_with_owner_index(aiohttp_client, storage):
    app = create_app(storage, server_id=0, n_servers=3)
    c = await aiohttp_client(app)
    key = next(f"k{i}".encode() for i in range(100) if owner_of(f"k{i}".encode(), 3) == 2)
    r = await c.put(url(key), data=b"v")
    assert r.status == 421
    assert await r.text() == "WRONG_OWNER 2"
    r = await c.get(url(key))
    assert r.status == 421
    assert key not in storage.data


async def test_commit_failure_is_500(aiohttp_client, tmp_path):
    import sqlite3
    from kvstore.storage import connect

    class FailingCommit:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, params)

        def close(self):
            self._conn.close()

    fatal = []
    s = Storage(str(tmp_path / "kv.db"), connect=lambda p: FailingCommit(connect(p)), on_fatal=fatal.append)
    s.load()
    s.start()
    c = await aiohttp_client(create_app(s, server_id=0, n_servers=1))
    r = await c.put(url(b"k"), data=b"v")
    assert r.status == 500
    assert "commit failed" in await r.text()
    assert fatal
    s.close()
```

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_server.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kvstore.server'`

**Step 3: Implement**

`kvstore/server.py`:
```python
"""HTTP API (design.md §5) and server entry point (design.md §6, §7)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from concurrent.futures import Future

from aiohttp import web

from .config import load_config, owner_of
from .keycodec import MAX_VALUE, decode_key
from .storage import Storage

STORAGE = web.AppKey("storage", Storage)
SERVER_ID = web.AppKey("server_id", int)
N_SERVERS = web.AppKey("n_servers", int)
OCTET_STREAM = "application/octet-stream"


def create_app(storage: Storage, server_id: int, n_servers: int) -> web.Application:
    app = web.Application(client_max_size=MAX_VALUE)
    app[STORAGE], app[SERVER_ID], app[N_SERVERS] = storage, server_id, n_servers
    app.add_routes([
        web.route("*", "/keys/", handle_empty_key),
        web.get("/keys/{key}", handle_get),
        web.put("/keys/{key}", handle_put),
        web.delete("/keys/{key}", handle_delete),
    ])
    return app


def _key(request: web.Request) -> bytes:
    try:
        key = decode_key(request.match_info["key"])
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"bad key: {exc}")
    owner = owner_of(key, request.app[N_SERVERS])
    if owner != request.app[SERVER_ID]:
        raise web.HTTPMisdirectedRequest(text=f"WRONG_OWNER {owner}")
    return key


async def _await_commit(fut: Future) -> None:
    try:
        await asyncio.wrap_future(fut)
    except Exception as exc:  # noqa: BLE001 - surfaced as 500; the process is exiting
        raise web.HTTPInternalServerError(text=f"commit failed: {exc}")


async def handle_empty_key(request: web.Request) -> web.Response:
    raise web.HTTPBadRequest(text="bad key: empty key")


async def handle_get(request: web.Request) -> web.Response:
    value = request.app[STORAGE].data.get(_key(request))
    if value is None:
        raise web.HTTPNotFound(text="NotFound")
    return web.Response(body=value, content_type=OCTET_STREAM)


async def handle_put(request: web.Request) -> web.Response:
    key = _key(request)
    value = await request.read()          # aiohttp returns 413 above client_max_size
    if len(value) > MAX_VALUE:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_VALUE, actual_size=len(value), text="value too large")
    await _await_commit(request.app[STORAGE].put(key, value))
    return web.Response(status=200)


async def handle_delete(request: web.Request) -> web.Response:
    await _await_commit(request.app[STORAGE].delete(_key(request)))
    return web.Response(status=200)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kvserver")
    parser.add_argument("--config", required=True)
    parser.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    me = cfg.servers[args.id]
    os.makedirs(me.data_dir, exist_ok=True)

    storage = Storage(os.path.join(me.data_dir, "kv.db"), max_batch=cfg.max_batch)
    storage.load()                        # 1-2: open (WAL replay) and load committed rows
    storage.start()                       # 3: writer thread
    print(f"kvserver {me.id}: loaded {len(storage.data)} keys, listening on {me.host}:{me.port}",
          file=sys.stderr, flush=True)
    web.run_app(create_app(storage, me.id, cfg.n), host=me.host, port=me.port,
                access_log=None, print=None)   # 4: listen only after the dict is complete


if __name__ == "__main__":
    main()
```

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_server.py -q`
Expected: `12 passed`

If `test_value_at_limit_ok_and_over_limit_413` fails with 413 on the exact-limit body, aiohttp's comparison is strict `>` in current versions; if your version differs, set `client_max_size=MAX_VALUE + 1` and keep the explicit length check.

**Step 5: Manual curl check**

```bash
mkdir -p data/single
.venv/bin/python -m kvstore.server --config cluster-1.json --id 0 &
sleep 1
curl -s -o /dev/null -w '%{http_code}\n' -X PUT --data-binary 'running' http://127.0.0.1:7300/keys/am9iLTE   # 200
curl -s -w ' %{http_code}\n' http://127.0.0.1:7300/keys/am9iLTE                                            # running 200
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://127.0.0.1:7300/keys/am9iLTE                    # 200
curl -s -w ' %{http_code}\n' http://127.0.0.1:7300/keys/am9iLTE                                            # NotFound 404
kill %1
```

**Step 6: Commit**

```bash
git add kvstore/server.py tests/test_server.py
git commit -m "feat: aiohttp server with design §5 status codes and startup order"
```

---

### Task 6: Client library

**Files:**
- Create: `kvstore/client.py`, `tests/test_client.py`

**Step 1: Write the failing tests**

`tests/test_client.py`:
```python
import pytest
from aiohttp.test_utils import TestServer

from kvstore.client import KVClient, ServerError, WrongOwner
from kvstore.config import ClusterConfig, ServerConfig, owner_of
from kvstore.server import create_app
from kvstore.storage import Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "kv.db"))
    s.load()
    s.start()
    yield s
    s.close()


@pytest.fixture
async def server(storage):
    srv = TestServer(create_app(storage, server_id=0, n_servers=1))
    await srv.start_server()
    yield srv
    await srv.close()


def config_for(srv, n=1) -> ClusterConfig:
    # Every entry points at the same test server; only entry 0 is the "real" owner.
    return ClusterConfig(servers=tuple(
        ServerConfig(id=i, host=srv.host, port=srv.port, data_dir=f"d{i}") for i in range(n)))


async def test_put_get_delete_roundtrip(server):
    async with KVClient(config_for(server)) as kv:
        await kv.put(b"job-1", b"running")
        assert await kv.get(b"job-1") == b"running"
        await kv.delete(b"job-1")
        assert await kv.get(b"job-1") is None
        await kv.delete(b"job-1")  # absent delete succeeds


async def test_empty_value_roundtrip_is_not_none(server):
    async with KVClient(config_for(server)) as kv:
        await kv.put(b"k", b"")
        assert await kv.get(b"k") == b""


async def test_binary_key_roundtrip(server):
    async with KVClient(config_for(server)) as kv:
        key = bytes(range(256))
        await kv.put(key, b"v")
        assert await kv.get(key) == b"v"


def test_url_routes_to_owner(server):
    cfg = ClusterConfig(servers=(
        ServerConfig(0, "h0", 1, "d0"), ServerConfig(1, "h1", 2, "d1"), ServerConfig(2, "h2", 3, "d2")))
    kv = KVClient(cfg)
    for i in range(50):
        key = f"k{i}".encode()
        owner = cfg.servers[owner_of(key, 3)]
        assert kv.url_for(key).startswith(f"http://{owner.host}:{owner.port}/keys/")


async def test_wrong_owner_raises(server, storage):
    # Config claims 2 servers, both at the test server which believes it is id 0.
    async with KVClient(config_for(server, n=2)) as kv:
        key = next(f"k{i}".encode() for i in range(100) if owner_of(f"k{i}".encode(), 2) == 1)
        with pytest.raises(WrongOwner) as exc:
            await kv.put(key, b"v")
        assert "WRONG_OWNER 1" in str(exc.value)


async def test_oversize_value_raises_server_error_413(server):
    async with KVClient(config_for(server)) as kv:
        with pytest.raises(ServerError) as exc:
            await kv.put(b"k", b"x" * (64 * 1024 + 1))
        assert exc.value.status == 413
```

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_client.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kvstore.client'`

**Step 3: Implement**

`kvstore/client.py`:
```python
"""Client library: routes each key to its owner and speaks the HTTP API (design.md §5, §6)."""
from __future__ import annotations

import aiohttp

from .config import ClusterConfig, owner_of
from .keycodec import encode_key


class ServerError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text}")
        self.status = status
        self.text = text


class WrongOwner(ServerError):
    """The server that received the request does not own the key (HTTP 421)."""


class KVClient:
    def __init__(self, config: ClusterConfig, conns_per_server: int = 32, timeout: float = 10.0):
        self.config = config
        self._conns_per_server = conns_per_server
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "KVClient":
        connector = aiohttp.TCPConnector(limit=0, limit_per_host=self._conns_per_server)
        self._session = aiohttp.ClientSession(connector=connector, timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def url_for(self, key: bytes) -> str:
        s = self.config.servers[owner_of(key, self.config.n)]
        return f"http://{s.host}:{s.port}/keys/{encode_key(key)}"

    async def put(self, key: bytes, value: bytes) -> None:
        async with self._session.put(self.url_for(key), data=value) as r:
            await self._raise_for_status(r)

    async def get(self, key: bytes) -> bytes | None:
        async with self._session.get(self.url_for(key)) as r:
            if r.status == 404:
                return None
            await self._raise_for_status(r)
            return await r.read()

    async def delete(self, key: bytes) -> None:
        async with self._session.delete(self.url_for(key)) as r:
            await self._raise_for_status(r)

    @staticmethod
    async def _raise_for_status(r: aiohttp.ClientResponse) -> None:
        if r.status == 200:
            return
        text = await r.text()
        if r.status == 421:
            raise WrongOwner(r.status, text)
        raise ServerError(r.status, text)
```

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_client.py -q`
Expected: `6 passed`

**Step 5: Commit**

```bash
git add kvstore/client.py tests/test_client.py
git commit -m "feat: async client library with md5 routing"
```

---

### Task 7: Start and stop scripts

**Files:**
- Create: `scripts/start.sh`, `scripts/stop.sh`, `tests/test_scripts.py`

**Step 1: Write the failing test**

`tests/test_scripts.py`:
```python
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
```

Add to `pytest.ini`:
```ini
markers =
    integration: starts real server processes
```

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_scripts.py -q`
Expected: FAIL with `FileNotFoundError` or non-zero return code (script missing)

**Step 3: Implement**

`scripts/start.sh`:
```bash
#!/usr/bin/env bash
# Start every server listed in a cluster config, in the background.
# usage: scripts/start.sh [cluster.json]
set -euo pipefail
CONFIG=${1:-cluster.json}
cd "$(dirname "$0")/.."
PY=${PYTHON:-.venv/bin/python}

N=$("$PY" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["servers"]))' "$CONFIG")
for i in $(seq 0 $((N - 1))); do
  read -r HOST PORT DIR < <("$PY" -c '
import json, sys
s = json.load(open(sys.argv[1]))["servers"][int(sys.argv[2])]
print(s["host"], s["port"], s["data_dir"])' "$CONFIG" "$i")
  mkdir -p "$DIR"
  if [ -f "$DIR/kvserver.pid" ] && kill -0 "$(cat "$DIR/kvserver.pid")" 2>/dev/null; then
    echo "server $i already running (pid $(cat "$DIR/kvserver.pid"))"; continue
  fi
  "$PY" -m kvstore.server --config "$CONFIG" --id "$i" >>"$DIR/kvserver.log" 2>&1 &
  echo $! >"$DIR/kvserver.pid"
  for _ in $(seq 1 200); do
    if "$PY" -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 0.2).close()' "$HOST" "$PORT" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  echo "server $i listening on $HOST:$PORT (pid $(cat "$DIR/kvserver.pid"))"
done
```

`scripts/stop.sh`:
```bash
#!/usr/bin/env bash
# Stop every server listed in a cluster config. Never touches kv.db, kv.db-wal or kv.db-shm.
# usage: scripts/stop.sh [cluster.json]
set -euo pipefail
CONFIG=${1:-cluster.json}
cd "$(dirname "$0")/.."
PY=${PYTHON:-.venv/bin/python}

for DIR in $("$PY" -c 'import json,sys; print("\n".join(s["data_dir"] for s in json.load(open(sys.argv[1]))["servers"]))' "$CONFIG"); do
  PIDFILE="$DIR/kvserver.pid"
  if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
      kill -TERM "$PID" && echo "stopped pid $PID ($DIR)"
    else
      echo "pid $PID ($DIR) not running"
    fi
    rm -f "$PIDFILE"
  fi
done
```

```bash
chmod +x scripts/start.sh scripts/stop.sh
```

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_scripts.py -q`
Expected: `1 passed`

**Step 5: Manual check with the default config**

```bash
scripts/start.sh            # 3 servers on 7300-7302
scripts/stop.sh
ls data/s0                  # kv.db kv.db-shm kv.db-wal kvserver.log; no pid file
```

**Step 6: Commit**

```bash
git add scripts pytest.ini tests/test_scripts.py
git commit -m "feat: start/stop scripts with pid files and port readiness wait"
```

---

### Task 8: Workload generator

**Files:**
- Create: `kvstore/bench.py`, `tests/test_bench.py`

Behaviour (requirement A5, design §8): open-loop at a fixed arrival rate, 80% Get / 15% Put / 5% Delete, uniform keys over K keys, 1 KiB values, 32 keep-alive connections per server, client-side latency including queueing, report p50/p95/p99/p999 per op, print pass/fail against read p99 < 50 ms and write p99 < 100 ms, write CSV.

**Step 1: Write the failing tests**

`tests/test_bench.py`:
```python
import pytest
from aiohttp.test_utils import TestServer

from kvstore.bench import READ_P99_MS, WRITE_P99_MS, Report, percentile, pick_op, run
from kvstore.config import ClusterConfig, ServerConfig
from kvstore.server import create_app
from kvstore.storage import Storage


def test_percentile_on_sorted_samples():
    samples = sorted(float(i) for i in range(1, 101))  # 1..100
    assert percentile(samples, 50) == 50.0
    assert percentile(samples, 99) == 99.0
    assert percentile(samples, 99.9) == 100.0
    assert percentile([], 99) is None


def test_pick_op_follows_80_15_5():
    assert pick_op(0.0) == "get" and pick_op(0.79) == "get"
    assert pick_op(0.80) == "put" and pick_op(0.94) == "put"
    assert pick_op(0.95) == "delete" and pick_op(0.999) == "delete"


def test_report_pass_fail_uses_targets():
    ok = Report(latencies={"get": [1.0] * 100, "put": [2.0] * 100, "delete": [2.0] * 100}, seconds=1, errors=0)
    assert ok.passed()
    slow_read = Report(latencies={"get": [READ_P99_MS + 1] * 100, "put": [1.0], "delete": [1.0]}, seconds=1, errors=0)
    assert not slow_read.passed()
    slow_write = Report(latencies={"get": [1.0], "put": [WRITE_P99_MS + 1] * 100, "delete": [1.0]}, seconds=1, errors=0)
    assert not slow_write.passed()
    errored = Report(latencies={"get": [1.0], "put": [1.0], "delete": [1.0]}, seconds=1, errors=1)
    assert not errored.passed()


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
```

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_bench.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kvstore.bench'`

**Step 3: Implement**

`kvstore/bench.py`:
```python
"""kvbench: open-loop workload generator (requirement A5, design.md §8).

usage: python -m kvstore.bench --config cluster.json --rate 1000 --seconds 60 --out results/n3.csv
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from dataclasses import dataclass, field

from .client import KVClient
from .config import ClusterConfig, load_config

READ_P99_MS = 50.0
WRITE_P99_MS = 100.0
OPS = ("get", "put", "delete")


def pick_op(r: float) -> str:
    if r < 0.80:
        return "get"
    if r < 0.95:
        return "put"
    return "delete"


def percentile(sorted_samples: list[float], pct: float) -> float | None:
    if not sorted_samples:
        return None
    idx = min(len(sorted_samples) - 1, max(0, int(round(pct / 100.0 * len(sorted_samples) + 0.5)) - 1))
    return sorted_samples[idx]


@dataclass
class Report:
    latencies: dict[str, list[float]]
    seconds: float
    errors: int
    rows: list[dict] = field(init=False)

    def __post_init__(self) -> None:
        self.rows = []
        for op in OPS:
            s = sorted(self.latencies.get(op, []))
            self.rows.append({
                "op": op, "count": len(s), "ops_per_sec": len(s) / self.seconds if self.seconds else 0.0,
                "p50_ms": percentile(s, 50), "p95_ms": percentile(s, 95), "p99_ms": percentile(s, 99),
                "p999_ms": percentile(s, 99.9), "max_ms": s[-1] if s else None,
            })

    def p99(self, op: str) -> float | None:
        return next(r["p99_ms"] for r in self.rows if r["op"] == op)

    def passed(self) -> bool:
        if self.errors:
            return False
        read_ok = (self.p99("get") or 0.0) < READ_P99_MS
        write_ok = all((self.p99(op) or 0.0) < WRITE_P99_MS for op in ("put", "delete"))
        return read_ok and write_ok

    def to_csv(self) -> str:
        cols = ["op", "count", "ops_per_sec", "p50_ms", "p95_ms", "p99_ms", "p999_ms", "max_ms"]
        fmt = lambda v: "" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
        return "\n".join([",".join(cols)] + [",".join(fmt(r[c]) for c in cols) for r in self.rows]) + "\n"

    def to_text(self) -> str:
        total = sum(r["count"] for r in self.rows)
        lines = [f"{'op':7s}{'count':>8s}{'ops/s':>9s}{'p50':>9s}{'p95':>9s}{'p99':>9s}{'p99.9':>9s}{'max':>9s}"]
        for r in self.rows:
            f = lambda v: "-" if v is None else f"{v:8.2f}"
            lines.append(f"{r['op']:7s}{r['count']:8d}{r['ops_per_sec']:9.0f}{f(r['p50_ms'])}{f(r['p95_ms'])}"
                         f"{f(r['p99_ms'])}{f(r['p999_ms'])}{f(r['max_ms'])}")
        lines.append(f"total {total} ops in {self.seconds:.0f}s = {total / self.seconds:.0f} ops/s, errors={self.errors}")
        lines.append(f"targets: read p99 < {READ_P99_MS:.0f} ms, write p99 < {WRITE_P99_MS:.0f} ms -> "
                     + ("PASS" if self.passed() else "FAIL"))
        return "\n".join(lines)


async def run(cfg: ClusterConfig, rate: int, seconds: float, n_keys: int = 100_000,
              value_size: int = 1024, conns_per_server: int = 32, seed: int | None = None) -> Report:
    rng = random.Random(seed)
    keys = [f"key-{i:08d}".encode() for i in range(n_keys)]          # uniform key space
    value = os.urandom(value_size)
    latencies: dict[str, list[float]] = {op: [] for op in OPS}
    errors = 0
    q: asyncio.Queue = asyncio.Queue()

    async def worker(kv: KVClient) -> None:
        nonlocal errors
        while True:
            item = await q.get()
            if item is None:
                return
            op, enqueued = item
            key = keys[rng.randrange(n_keys)]
            try:
                if op == "get":
                    await kv.get(key)
                elif op == "put":
                    await kv.put(key, value)
                else:
                    await kv.delete(key)
                latencies[op].append((time.perf_counter() - enqueued) * 1000.0)
            except Exception:  # noqa: BLE001 - counted, run continues
                errors += 1

    async with KVClient(cfg, conns_per_server=conns_per_server) as kv:
        workers = [asyncio.create_task(worker(kv)) for _ in range(conns_per_server * cfg.n)]
        interval = 1.0 / rate
        start = time.perf_counter()
        next_at, end = start, start + seconds
        while next_at < end:                                           # open loop
            q.put_nowait((pick_op(rng.random()), time.perf_counter()))
            next_at += interval
            delay = next_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        for _ in workers:
            q.put_nowait(None)
        await asyncio.gather(*workers)
        elapsed = time.perf_counter() - start
    return Report(latencies=latencies, seconds=elapsed, errors=errors)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kvbench")
    p.add_argument("--config", default="cluster.json")
    p.add_argument("--rate", type=int, default=1000, help="target ops/s (open loop)")
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--keys", type=int, default=100_000)
    p.add_argument("--value-size", type=int, default=1024)
    p.add_argument("--conns", type=int, default=32, help="keep-alive connections per server")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--out", default=None, help="CSV path")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    print(f"kvbench: {cfg.n} server(s), {a.rate} ops/s for {a.seconds:.0f}s, 80/15/5, "
          f"{a.keys} keys, {a.value_size} B values, {a.conns} conns/server", file=sys.stderr, flush=True)
    rep = asyncio.run(run(cfg, a.rate, a.seconds, a.keys, a.value_size, a.conns, a.seed))
    print(rep.to_text())
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w") as f:
            f.write(rep.to_csv())
        print(f"csv written to {a.out}")
    return 0 if rep.passed() else 1


if __name__ == "__main__":
    sys.exit(main())
```

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_bench.py -q`
Expected: `5 passed`

**Step 5: Short real run**

```bash
scripts/start.sh cluster-1.json
.venv/bin/python -m kvstore.bench --config cluster-1.json --rate 1000 --seconds 10
scripts/stop.sh cluster-1.json
```
Expected: a table with get/put/delete rows and `-> PASS`; exit code 0.

**Step 6: Commit**

```bash
git add kvstore/bench.py tests/test_bench.py
git commit -m "feat: kvbench open-loop workload generator with percentile report"
```

---

### Task 9: Restart test (requirement A8)

**Files:**
- Create: `tests/test_restart.py`

Procedure: start one server as a subprocess, run writer tasks on disjoint key ranges, SIGKILL the server mid-load, restart it on the same data dir, verify every key.

The subtle case: a Put may COMMIT and then the server is killed before the 200 is sent. The client sees an error, but after restart Get returns the new value. That is correct behaviour, not a bug. To make the check sound:
- Each writer records, before sending, the operation as *pending* for its key, and promotes it to *acked* when the response arrives.
- Each writer stops at its first error, so each key has at most one pending operation.
- Verification accepts the acked value **or** the pending value (a pending Delete means `NotFound` is also valid). Any other result is a bug.

**Step 1: Write the test**

`tests/test_restart.py`:
```python
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

    assert len(acked) > 50
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
```

**Step 2: Run it**

Run: `.venv/bin/pytest tests/test_restart.py -q -s`
Expected: `1 passed`. If it fails, the failure is a real durability bug: do not weaken the test. Check that the server is listening only after `load()` and that the dict is applied only after COMMIT. A Get that returns neither the acked nor the pending value is the only failure condition.

**Step 3: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all passed (about 54 tests).

**Step 4: Commit**

```bash
git add tests/test_restart.py
git commit -m "test: SIGKILL mid-load restart test for acked writes"
```

---

### Task 10: Final measurement and README

**Files:**
- Create: `README.md`, `results/n1.csv`, `results/n3.csv` (remove `results/` from `.gitignore` so the CSVs are committed)

**Step 1: Fresh data, N=1, 60 s**

```bash
rm -rf data
scripts/start.sh cluster-1.json
.venv/bin/python -m kvstore.bench --config cluster-1.json --rate 1000 --seconds 60 --out results/n1.csv
scripts/stop.sh cluster-1.json
```
Expected: `-> PASS`, exit 0.

**Step 2: Fresh data, N=3, 60 s**

```bash
rm -rf data
scripts/start.sh cluster.json
.venv/bin/python -m kvstore.bench --config cluster.json --rate 1000 --seconds 60 --out results/n3.csv
scripts/stop.sh cluster.json
```
Expected: `-> PASS`, exit 0.

**Step 3: Restart demo on the N=3 cluster**

```bash
scripts/start.sh cluster.json
.venv/bin/python -m kvstore.bench --config cluster.json --rate 1000 --seconds 5
kill -9 "$(cat data/s1/kvserver.pid)"
scripts/start.sh cluster.json            # restarts only server 1; others report "already running"
.venv/bin/python -m kvstore.bench --config cluster.json --rate 1000 --seconds 5
scripts/stop.sh cluster.json
```
Expected: second start prints `server 1 listening`; both bench runs PASS. Also run `.venv/bin/pytest tests/test_restart.py -q` and record the result.

**Step 4: Write `README.md`**

```markdown
# Distributed Key-Value Store (v1)

Hash-partitioned Python key-value store: 1–4 servers, each one asyncio process with a SQLite
database (WAL, `synchronous=FULL`, `fullfsync=ON`) and an in-memory copy of committed state.
Writes are group-committed and acknowledged only after the transaction is durable.

Docs: [requirements](docs/requirement.md) · [design](docs/design.md) ·
[spike measurements](spike/README.md)

## Quickstart

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    scripts/start.sh                 # 3 servers from cluster.json on 127.0.0.1:7300-7302
    curl -X PUT --data-binary 'running' http://127.0.0.1:7300/keys/am9iLTE   # key "job-1"
    curl http://127.0.0.1:7300/keys/am9iLTE
    scripts/stop.sh

The key in the URL is base64url. A server answers `421 WRONG_OWNER <id>` if the key hashes
to another server; the client library (`kvstore.client.KVClient`) routes automatically.

## API

| Op | Request | Success | Errors |
|----|---------|---------|--------|
| Put | `PUT /keys/{b64url}` body = value | 200 | 400, 413, 421, 500 |
| Get | `GET /keys/{b64url}` | 200 + body (empty allowed) | 404 `NotFound`, 400, 421 |
| Delete | `DELETE /keys/{b64url}` | 200 (also if absent) | 400, 421, 500 |

## Tests

    .venv/bin/pytest -q                       # unit + integration (starts real processes)
    .venv/bin/pytest -q -m "not integration"  # unit only

## Workload generator

    .venv/bin/python -m kvstore.bench --config cluster.json --rate 1000 --seconds 60 --out results/n3.csv

Open loop at a fixed arrival rate, 80% Get / 15% Put / 5% Delete, uniform keys over 100K,
1 KiB values, 32 keep-alive connections per server. Latency is measured client-side and
includes queueing. Exit code 0 means read p99 < 50 ms and write p99 < 100 ms.

## Results

Machine: <fill in: model, OS, Python version>. Date: <fill in>. 60 s at 1,000 ops/s.

| Servers | Get p99 | Put p99 | Delete p99 | Get p99.9 | Put p99.9 | Errors | Result |
|---------|---------|---------|------------|-----------|-----------|--------|--------|
| 1 | <ms> | <ms> | <ms> | <ms> | <ms> | 0 | PASS |
| 3 | <ms> | <ms> | <ms> | <ms> | <ms> | 0 | PASS |

Raw CSVs: [results/n1.csv](results/n1.csv), [results/n3.csv](results/n3.csv).

Restart test: `tests/test_restart.py` SIGKILLs a server under load, restarts it and verifies
every acknowledged write. Result: <PASS, date>.

## Layout

    kvstore/config.py    cluster.json loading, owner_of = md5(key) % N
    kvstore/keycodec.py  base64url key codec, size limits
    kvstore/storage.py   SQLite + writer thread, batched commits, apply-after-commit, fail-stop
    kvstore/server.py    aiohttp handlers and startup order
    kvstore/client.py    async client with routing
    kvstore/bench.py     kvbench workload generator
    scripts/             start.sh / stop.sh
    spike/               feasibility measurements that drove the design
```

Fill every `<...>` from the CSVs and `uname -a`, `sw_vers`, `.venv/bin/python --version`.

**Step 5: Commit**

```bash
sed -i '' '/^results\/$/d' .gitignore
git add README.md results .gitignore
git commit -m "docs: README with quickstart and measured p99 results for N=1 and N=3"
```

---

## Verification checklist (end of plan)

- [ ] `.venv/bin/pytest -q` all green, including integration and restart tests.
- [ ] `scripts/start.sh` then `scripts/stop.sh` on `cluster.json` and `cluster-1.json` both clean; `kv.db*` files remain.
- [ ] `results/n1.csv` and `results/n3.csv` show read p99 < 50 ms and write p99 < 100 ms at 1,000 ops/s over 60 s, errors 0.
- [ ] README results table filled from the CSVs.
- [ ] `docs/design.md` and `docs/requirement.md` unchanged (`git diff --stat HEAD~N -- docs/design.md docs/requirement.md` is empty).
