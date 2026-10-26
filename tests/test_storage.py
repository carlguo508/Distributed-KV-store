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


# Checks: every connection runs with the durable pragmas (design.md §6, §8 "Durability pragma").
# Setup: a fresh database path.
# Action: open it with connect() and read back each pragma.
# Expected: journal_mode wal, synchronous 2 (FULL), fullfsync 1, checkpoint_fullfsync 1.
def test_connect_sets_durable_pragmas(db_path):
    conn = connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1
    assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
    conn.close()


# Checks: put returns a future, and once it resolves the value is readable (design.md §3 Put, §4 row 1).
# Setup: a started storage on an empty database.
# Action: put k=v and wait for the future.
# Expected: the future is a concurrent.futures.Future and data[k] == v afterwards.
def test_put_returns_future_and_is_visible_after_result(storage):
    fut = storage.put(b"k", b"v")
    assert isinstance(fut, Future)
    fut.result(timeout=5)
    assert storage.data[b"k"] == b"v"


# Checks: an empty value is valid and distinct from a missing key (requirement F4).
# Setup: a started storage on an empty database.
# Action: put k with an empty value and wait.
# Expected: data[k] is b"" (present), while a never-written key is absent.
def test_empty_value_is_stored_and_distinct_from_missing(storage):
    storage.put(b"k", b"").result(timeout=5)
    assert storage.data[b"k"] == b""
    assert b"other" not in storage.data


# Checks: delete removes a key, and deleting an absent key succeeds (requirement F3, design.md §3).
# Setup: a started storage with k=v written.
# Action: delete k, then delete k again.
# Expected: k is gone after the first delete; the second delete's future resolves without error.
def test_delete_removes_and_deleting_absent_succeeds(storage):
    storage.put(b"k", b"v").result(timeout=5)
    storage.delete(b"k").result(timeout=5)
    assert b"k" not in storage.data
    storage.delete(b"k").result(timeout=5)  # absent: still succeeds


# Checks: committed writes survive closing and reopening the database (requirement restart recovery, design.md §7).
# Setup: a storage that commits a=1, b=2, then deletes a, then is closed.
# Action: open a new Storage on the same file and load().
# Expected: the loaded dict is exactly {b: 2}.
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


# Checks: writes to one key are applied in submission (FIFO) order (design.md §4, concurrent Put/Put and Put/Delete).
# Setup: a started storage.
# Action: submit 50 puts, a delete and a final put on the same key without waiting in between, then wait for all.
# Expected: the last submitted write wins, so data[k] == b"final".
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


# Checks: apply-after-commit, so no reader sees a value before its COMMIT returns (design.md §1 Visibility, §3 Get).
# Setup: a storage whose connection pauses inside COMMIT until the test releases it.
# Action: put k=v, wait until the writer is inside COMMIT, inspect state, then release COMMIT.
# Expected: while COMMIT is held, k is absent and the future is not done; after release, data[k] == v.
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


# Checks: group commit, so many queued writes share few transactions (design.md §1 Durability, §2 writer thread).
# Setup: a storage whose first COMMIT is held, so later writes pile up in the queue.
# Action: submit 1 write, wait until it is in COMMIT, submit 100 more, release, wait for all.
# Expected: at most 3 COMMITs in total and all 101 keys present.
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
    def __init__(self, conn, attempts=None):
        self._conn = conn
        self._attempts = attempts if attempts is not None else []

    def execute(self, sql, params=()):
        if sql == "COMMIT":
            self._attempts.append(1)
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, params)

    def close(self):
        self._conn.close()


# Checks: fail-stop on COMMIT failure (design.md §1 Failure policy, §4 last row, §7).
# Setup: a storage whose COMMIT always raises "disk I/O error", with on_fatal recorded instead
#   of exiting. Both puts are queued BEFORE the writer starts, so its first drain takes both
#   into one batch. If they were split, the writer would stop after the first failed batch and
#   the second future would never resolve.
# Action: start the writer and wait for both futures.
# Expected: exactly one COMMIT attempt (one batch), both futures raise the error, nothing is
#   applied to data, and on_fatal is called once with it.
def test_commit_failure_fails_batch_applies_nothing_and_calls_on_fatal(db_path):
    fatal, attempts = [], []
    s = Storage(db_path, connect=lambda p: FailingCommit(connect(p), attempts), on_fatal=fatal.append)
    s.load()
    f1, f2 = s.put(b"a", b"1"), s.put(b"b", b"2")
    s.start()
    with pytest.raises(sqlite3.OperationalError):
        f1.result(timeout=5)
    with pytest.raises(sqlite3.OperationalError):
        f2.result(timeout=5)
    assert len(attempts) == 1           # both writes were in the same failed batch
    assert s.data == {}
    assert len(fatal) == 1 and isinstance(fatal[0], sqlite3.OperationalError)
    s.close()
