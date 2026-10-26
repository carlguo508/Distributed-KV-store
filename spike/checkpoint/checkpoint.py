"""Offline manual checkpoint: copy every WAL frame into kv.db and truncate the WAL to 0 bytes.
Run only while the server that owns the database is stopped.

usage: checkpoint.py <kv.db> [<kv.db> ...]
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kvstore.storage import connect  # noqa: E402  (same durable pragmas as the server)

for path in sys.argv[1:]:
    wal = path + "-wal"
    before = os.path.getsize(wal) if os.path.exists(wal) else 0
    conn = connect(path)
    busy, frames, copied = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    conn.close()
    after = os.path.getsize(wal) if os.path.exists(wal) else 0
    print(f"{path}: busy={busy} frames={frames} copied={copied} wal {before} -> {after} bytes")
    if busy:
        sys.exit(f"checkpoint of {path} was blocked; is a server still running?")
