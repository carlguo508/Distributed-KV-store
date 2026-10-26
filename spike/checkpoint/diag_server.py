"""Diagnostic kvserver: identical startup and request handling to kvstore.server.main, but the
writer connection logs one CSV row per transaction.

usage: diag_server.py <cluster.json> <id> <commits.csv> [PRAGMA ...]
       e.g. diag_server.py cluster.json 0 s0.csv wal_autocheckpoint=0

Columns: t (wall clock, s), batch (statements), exec_ms (BEGIN + statements), commit_ms (COMMIT,
including fsync and any auto-checkpoint), ckpt (1 if kv.db changed during the COMMIT; in WAL mode
only a checkpoint writes the main file), wal_bytes (size of kv.db-wal after the COMMIT).
Extra PRAGMAs are applied to the writer connection after the normal durable ones.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

from kvstore import storage as st  # noqa: E402
from kvstore.config import load_config  # noqa: E402
from kvstore.server import create_app  # noqa: E402

cfg_path, sid, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
extra = sys.argv[4:]
log = open(out, "w", buffering=1)
log.write("t,batch,exec_ms,commit_ms,ckpt,wal_bytes\n")


class TimedConnection:
    def __init__(self, conn, path):
        self._c, self._db, self._wal = conn, path, path + "-wal"
        self._n, self._t_begin = 0, 0.0

    def execute(self, sql, params=()):
        if sql == "BEGIN":
            self._n, self._t_begin = 0, time.perf_counter()
            return self._c.execute(sql, params)
        if sql == "COMMIT":
            exec_ms = (time.perf_counter() - self._t_begin) * 1000
            m0 = os.stat(self._db).st_mtime_ns
            t = time.perf_counter()
            r = self._c.execute(sql, params)
            commit_ms = (time.perf_counter() - t) * 1000
            ckpt = int(os.stat(self._db).st_mtime_ns != m0)
            wal = os.stat(self._wal).st_size if os.path.exists(self._wal) else 0
            log.write(f"{time.time():.3f},{self._n},{exec_ms:.3f},{commit_ms:.3f},{ckpt},{wal}\n")
            return r
        self._n += 1
        return self._c.execute(sql, params)

    def close(self):
        self._c.close()


def connect(path):
    c = st.connect(path)
    for p in extra:
        c.execute(f"PRAGMA {p}")
    return TimedConnection(c, path)


cfg = load_config(cfg_path)
me = cfg.servers[sid]
os.makedirs(me.data_dir, exist_ok=True)
s = st.Storage(os.path.join(me.data_dir, "kv.db"), max_batch=cfg.max_batch, connect=connect)
s.load()
s.start()

# Candidate fix under test (env BG_CKPT=<seconds>): the writer never checkpoints inside COMMIT
# (run with the wal_autocheckpoint=0 PRAGMA); a separate connection runs a PASSIVE checkpoint on
# this interval. PASSIVE never blocks the writer, but it can only copy frames that are not still
# needed, and the WAL file is reset only when a checkpoint completes while no write is pending.
bg = os.environ.get("BG_CKPT")
if bg:
    import threading

    def checkpointer():
        db = os.path.join(me.data_dir, "kv.db")
        c = st.connect(db)
        clog = open(out.replace(".commits.csv", ".bgckpt.csv"), "w", buffering=1)
        clog.write("t,ckpt_ms,busy,wal_frames,copied,wal_bytes\n")
        while True:
            time.sleep(float(bg))
            t = time.perf_counter()
            busy, frames, copied = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            ms = (time.perf_counter() - t) * 1000
            wal = os.path.getsize(db + "-wal") if os.path.exists(db + "-wal") else 0
            clog.write(f"{time.time():.3f},{ms:.3f},{busy},{frames},{copied},{wal}\n")

    threading.Thread(target=checkpointer, name="bg-checkpoint", daemon=True).start()

print(f"diag server {sid}: loaded {len(s.data)} keys, extra pragmas {extra}", file=sys.stderr, flush=True)
web.run_app(create_app(s, me.id, cfg.n), host=me.host, port=me.port, access_log=None, print=None)
