"""Spike: aiohttp + SQLite (WAL, synchronous=FULL, fullfsync=ON) with batched commits.
usage: python http_server.py <port> <db_path> <num_servers> <server_index>

Routes: PUT/GET/DELETE /keys/{base64url key}. Value is the raw body.
Writes go to a single writer thread that drains a queue, commits one transaction per
batch, and only then applies the batch to the in-memory dict and wakes the waiters
(apply-after-commit). If the commit fails nothing is applied and callers get 500.
"""
import asyncio, base64, hashlib, queue, sqlite3, sys, threading
from aiohttp import web

PORT, DB, N, IDX = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
MAX_BATCH = 256
store = {}                      # key bytes -> value bytes, committed state only
wq = queue.Queue()              # (op, key, value, future, loop)

def open_db():
    c = sqlite3.connect(DB, isolation_level=None, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=FULL")
    c.execute("PRAGMA fullfsync=ON"); c.execute("PRAGMA checkpoint_fullfsync=ON")
    c.execute("CREATE TABLE IF NOT EXISTS kv(k BLOB PRIMARY KEY, v BLOB NOT NULL) WITHOUT ROWID")
    return c

def writer():
    c = open_db()
    while True:
        batch = [wq.get()]
        try:
            while len(batch) < MAX_BATCH: batch.append(wq.get_nowait())
        except queue.Empty: pass
        try:
            c.execute("BEGIN")
            for op, k, v, _, _ in batch:
                if op == "put": c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))
                else: c.execute("DELETE FROM kv WHERE k=?", (k,))
            c.execute("COMMIT")
        except Exception as e:                       # fsync or SQL failure: apply nothing
            try: c.execute("ROLLBACK")
            except Exception: pass
            for *_, fut, loop in batch: loop.call_soon_threadsafe(fut.set_exception, e)
            continue
        for op, k, v, fut, loop in batch:            # durable: now apply in log order
            if op == "put": store[k] = v
            else: store.pop(k, None)
            loop.call_soon_threadsafe(fut.set_result, None)

def owner(key): return int(hashlib.md5(key).hexdigest(), 16) % N

def decode_key(req):
    try:
        key = base64.urlsafe_b64decode(req.match_info["key"] + "==")
    except Exception:
        raise web.HTTPBadRequest(text="bad key encoding")
    if not key or len(key) > 256: raise web.HTTPBadRequest(text="key length")
    if owner(key) != IDX: raise web.HTTPMisdirectedRequest(text="WRONG_OWNER")
    return key

async def durable(op, key, val=b""):
    loop = asyncio.get_running_loop(); fut = loop.create_future()
    wq.put((op, key, val, fut, loop))
    try: await fut
    except Exception as e: raise web.HTTPInternalServerError(text=f"commit failed: {e}")

async def get(req):
    v = store.get(decode_key(req))
    if v is None: raise web.HTTPNotFound(text="NotFound")
    return web.Response(body=v, content_type="application/octet-stream")
async def put(req):
    key = decode_key(req); val = await req.read()
    if len(val) > 65536: raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=len(val))
    await durable("put", key, val); return web.Response(status=200)
async def delete(req):
    await durable("del", decode_key(req)); return web.Response(status=200)

def load():
    c = open_db()
    for k, v in c.execute("SELECT k, v FROM kv"): store[k] = v
    c.close()

if __name__ == "__main__":
    load()
    threading.Thread(target=writer, daemon=True).start()
    app = web.Application(client_max_size=65536 + 1024)
    app.add_routes([web.get("/keys/{key}", get), web.put("/keys/{key}", put), web.delete("/keys/{key}", delete)])
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=None)
