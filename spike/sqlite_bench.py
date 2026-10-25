import sqlite3, os, time, sys
print("sqlite", sqlite3.sqlite_version)
val = os.urandom(1024)
def bench(fullfsync, batch, n=600):
    for f in ("b.db","b.db-wal","b.db-shm"):
        try: os.unlink(f)
        except FileNotFoundError: pass
    c = sqlite3.connect("b.db", isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=FULL")
    c.execute(f"PRAGMA fullfsync={'ON' if fullfsync else 'OFF'}")
    c.execute("PRAGMA checkpoint_fullfsync=ON" if fullfsync else "PRAGMA checkpoint_fullfsync=OFF")
    c.execute("CREATE TABLE kv(k BLOB PRIMARY KEY, v BLOB) WITHOUT ROWID")
    lat = []
    for i in range(n // batch):
        t = time.perf_counter()
        c.execute("BEGIN")
        for j in range(batch):
            c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (os.urandom(32), val))
        c.execute("COMMIT")
        lat.append((time.perf_counter() - t) * 1000)
    lat.sort(); m = len(lat)
    print(f"fullfsync={'ON ' if fullfsync else 'OFF'} batch={batch:3d}  commit p50={lat[m//2]:6.2f}ms p99={lat[int(m*.99)]:6.2f}ms max={lat[-1]:6.2f}ms  per-row={sum(lat)/n:.3f}ms")
    c.close()
for ff in (False, True):
    for b in (1, 5, 20):
        bench(ff, b)
