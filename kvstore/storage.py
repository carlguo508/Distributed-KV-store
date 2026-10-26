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

# design.md §1 Storage, §6: WAL for crash recovery and concurrent startup read; FULL sync
# so COMMIT waits for the WAL to reach disk; fullfsync because a plain fsync on macOS
# only reaches the drive cache (spike measured F_FULLFSYNC ~4 ms); checkpoint_fullfsync
# so copying the WAL back into kv.db is just as durable.
_PRAGMAS = ("journal_mode=WAL", "synchronous=FULL", "fullfsync=ON", "checkpoint_fullfsync=ON")
# design.md §2: WITHOUT ROWID stores rows in the primary-key B-tree directly, so a
# lookup by key needs no second index.
# Checkpointing (spike/checkpoint/README.md). SQLite's default auto-checkpoint runs inside the
# COMMIT that crosses 1,000 WAL pages, so every write queued behind that COMMIT waits for the copy
# into kv.db; the spike measured this as the main source of write-tail spikes. Instead:
#  - a background connection copies WAL pages into kv.db with PASSIVE checkpoints, which never
#    block the writer;
#  - PASSIVE alone never resets the WAL under steady writes (a reset needs a moment when every
#    frame is copied and no write is pending), so the writer bounds it: when the WAL file passes
#    the limit, it runs a TRUNCATE checkpoint between batches. Most pages were already copied by
#    the background thread, so this stall is short, and it happens only once per ~limit bytes.
DEFAULT_CHECKPOINT_INTERVAL = 1.0           # seconds between background PASSIVE checkpoints
DEFAULT_WAL_LIMIT_BYTES = 64 * 1024 * 1024  # writer truncates the WAL above this size
_SCHEMA = "CREATE TABLE IF NOT EXISTS kv(k BLOB PRIMARY KEY, v BLOB NOT NULL) WITHOUT ROWID"


def connect(path: str):
    # isolation_level=None: Python's sqlite3 must not open transactions implicitly;
    # the writer issues BEGIN/COMMIT itself so one batch is exactly one transaction.
    # check_same_thread=False: the writer connection is opened by start() on the caller's
    # thread but used only by the writer thread afterwards.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    for pragma in _PRAGMAS:
        conn.execute(f"PRAGMA {pragma}")
    conn.execute(_SCHEMA)
    assert_durable(conn)
    return conn


def assert_durable(conn) -> None:
    """Refuse to run if the durability pragmas did not take effect (design.md §8).
    A SQLite build could silently ignore a pragma, and every ack would then be a lie."""
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    fullfsync = conn.execute("PRAGMA fullfsync").fetchone()[0]
    if synchronous != 2 or fullfsync != 1:
        raise RuntimeError(f"durability pragmas not in effect: synchronous={synchronous} fullfsync={fullfsync}")


def _default_on_fatal(exc: BaseException) -> None:
    # design.md §1 Failure policy: after a failed COMMIT the disk state is uncertain, so
    # the process exits and a restart reloads the last committed state (§7).
    # os._exit, not sys.exit: this runs on the writer thread, where sys.exit would only
    # end the thread and leave the server up with no writer.
    print(f"kvstore: commit failed, exiting: {exc!r}", file=sys.stderr, flush=True)
    os._exit(1)


@dataclass
class _Record:
    op: str
    key: bytes
    value: bytes
    # concurrent.futures.Future, not an asyncio future: it is resolved from the writer
    # thread and is thread-safe. The server awaits it with asyncio.wrap_future.
    future: Future


class Storage:
    """Owns the committed-state dict and the writer thread.

    Invariant: `data` always equals the committed content of the database.
    Only `load()` (before `start()`) and the writer thread ever mutate it.
    """

    # `connect` and `on_fatal` are injectable so tests can simulate a slow or failing
    # COMMIT and observe fail-stop without killing the test process (design.md §8 Unit).
    def __init__(self, path: str, max_batch: int = 256,
                 connect: Callable[[str], object] = connect,
                 on_fatal: Callable[[BaseException], None] = _default_on_fatal,
                 checkpoint_interval: float = DEFAULT_CHECKPOINT_INTERVAL,
                 wal_limit_bytes: int = DEFAULT_WAL_LIMIT_BYTES):
        self.path = path
        self.max_batch = max_batch
        self.checkpoint_interval = checkpoint_interval
        self.wal_limit_bytes = wal_limit_bytes
        self._wal_path = path + "-wal"
        self._connect = connect
        self._on_fatal = on_fatal
        # design.md §2: a full in-memory copy of committed state; reads never touch SQLite.
        self.data: dict[bytes, bytes] = {}
        # design.md §2: FIFO queue between the event loop and the writer; its order is
        # the single per-key order that gives linearizability (§4).
        self._queue: queue.Queue = queue.Queue()
        self._conn = None
        self._thread: threading.Thread | None = None
        self._checkpointer: threading.Thread | None = None
        self._stop_checkpointer = threading.Event()

    # ---- lifecycle -------------------------------------------------------
    def load(self) -> None:
        """Open the database (replays the WAL) and load every committed row.

        design.md §7 steps 1-2. The read connection is closed right after, so no
        connection ever holds a read transaction that would block checkpoints.
        """
        conn = self._connect(self.path)
        try:
            self.data = {k: v for k, v in conn.execute("SELECT k, v FROM kv")}
        finally:
            conn.close()

    def start(self) -> None:
        """design.md §7 step 3: start the writer, plus the background checkpoint thread."""
        self._conn = self._connect(self.path)
        # The writer never checkpoints inside COMMIT; see the checkpointing note at the top.
        self._conn.execute("PRAGMA wal_autocheckpoint=0")
        # daemon: on SIGTERM the process may exit without draining the queue; queued
        # writes were never acked, so losing them is allowed (design.md §7 table).
        self._thread = threading.Thread(target=self._run, name="kv-writer", daemon=True)
        self._thread.start()
        self._stop_checkpointer.clear()
        self._checkpointer = threading.Thread(target=self._checkpoint_loop, name="kv-checkpoint",
                                              daemon=True)
        self._checkpointer.start()

    def close(self) -> None:
        if self._thread is not None:
            # None is the stop sentinel. It enters the FIFO behind every record already
            # queued, so those records are committed before the writer exits.
            self._queue.put(None)
            self._thread.join(timeout=10)
            self._thread = None
        if self._checkpointer is not None:
            self._stop_checkpointer.set()
            self._checkpointer.join(timeout=10)
            self._checkpointer = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- public write API ------------------------------------------------
    def put(self, key: bytes, value: bytes) -> Future:
        return self._submit(PUT, key, value)

    def delete(self, key: bytes) -> Future:
        # design.md §3: a delete of an absent key still goes through a batch so its ack
        # carries the same durability meaning as any other write.
        return self._submit(DELETE, key, b"")

    def _submit(self, op: str, key: bytes, value: bytes) -> Future:
        fut: Future = Future()
        self._queue.put(_Record(op, key, value, fut))
        return fut

    # ---- writer thread ---------------------------------------------------
    def _run(self) -> None:
        # Safety net: any unexpected exception (a bug, not a COMMIT failure, which _commit
        # handles) must not end the writer silently while handlers keep serving reads and
        # queueing writes that would never be acked. Fail the in-flight batch and treat it
        # like a commit failure (design.md §1 Failure policy).
        batch: list[_Record] = []
        try:
            self._loop(batch)
        except BaseException as exc:  # noqa: BLE001 - any writer crash is fatal
            for rec in batch:
                if not rec.future.done():
                    rec.future.set_exception(exc)
            self._on_fatal(exc)

    def _loop(self, batch: list[_Record]) -> None:
        # `batch` is owned by _run so it can fail the in-flight records if this raises.
        while True:
            batch.clear()
            # Block for the first record, then take whatever else is already queued
            # without waiting. Under light load a batch is one write (no added latency);
            # under heavy load writes that queued during the previous fsync share the
            # next one (group commit, design.md §1 Durability).
            first = self._queue.get()
            if first is None:
                return
            batch.append(self._take(first))
            stop = False
            while len(batch) < self.max_batch:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    stop = True
                    break
                batch.append(self._take(item))
            # After a failed commit the writer stops for good, so no later batch can
            # commit on top of uncertain disk state (design.md §4 last row).
            if not self._commit(batch):
                return
            # After the batch is acked, so its own requests never wait for this.
            self._bound_wal()
            if stop:
                return

    def _bound_wal(self) -> None:
        """Run a TRUNCATE checkpoint when the WAL file has grown past the limit. It runs on
        the writer between batches, so no write is pending and the WAL can be reset. An error
        propagates to _run and is fatal, like a failed COMMIT (design.md §1 Failure policy)."""
        try:
            size = os.path.getsize(self._wal_path)
        except FileNotFoundError:
            return
        if size > self.wal_limit_bytes:
            # busy=1 means the background PASSIVE checkpoint held the checkpoint lock past the
            # busy timeout; harmless, the next batch retries.
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    # ---- background checkpoint thread --------------------------------------
    def _checkpoint_loop(self) -> None:
        """Copy WAL pages into kv.db every checkpoint_interval with PASSIVE checkpoints, which
        never block the writer. It uses its own connection and holds no read transaction
        between checkpoints, so it never stops the writer's TRUNCATE from completing."""
        conn = None
        try:
            conn = self._connect(self.path)
            while not self._stop_checkpointer.wait(self.checkpoint_interval):
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        except BaseException as exc:  # noqa: BLE001 - an I/O error while checkpointing is fatal
            self._on_fatal(exc)
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _take(rec: _Record) -> _Record:
        # A future is cancelled when aiohttp cancels the handler awaiting it: on server
        # shutdown (e.g. SIGTERM), or on client disconnect if handler_cancellation is
        # enabled. asyncio.wrap_future propagates that cancel here. Marking it RUNNING here
        # means a later cancel() fails, so once the writer owns a record its future is
        # either already cancelled or can no longer become cancelled, and resolving it
        # cannot raise InvalidStateError. A cancelled record's write is still committed:
        # it was never acked, so applying it or not are both allowed (design.md §7 table).
        rec.future.set_running_or_notify_cancel()
        return rec

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
            # design.md §3 Put "COMMIT fails": every request in the batch gets an error
            # (500), and the dict is left untouched because apply only runs on success.
            for rec in batch:
                if not rec.future.cancelled():     # nobody is waiting for a cancelled one
                    rec.future.set_exception(exc)
            self._on_fatal(exc)
            return False
        # design.md §1 Visibility (apply-after-commit): only now, with the WAL fsynced,
        # do the writes become visible to readers. Applying in queue order makes the
        # dict agree with the last SQL statement for each key (§4).
        for rec in batch:
            if rec.op == PUT:
                self.data[rec.key] = rec.value
            else:
                self.data.pop(rec.key, None)
        # Acks go out after the dict is updated, so a Get sent after the ack sees the
        # write (design.md §4 row 1).
        for rec in batch:
            if not rec.future.cancelled():
                rec.future.set_result(None)
        return True
