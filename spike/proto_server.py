import asyncio, os, sys, struct, fcntl, time
MODE = sys.argv[1]           # "perwrite" | "group"
PORT = int(sys.argv[2])
LOG = sys.argv[3]
store = {}
fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
def fullsync(): fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
pending = []           # futures waiting for the next fsync
buf = []
async def syncer():
    while True:
        await asyncio.sleep(0)      # yield; batch whatever arrived
        if not buf:
            await asyncio.sleep(0.0005); continue
        data = b"".join(buf); waiters = pending[:]; buf.clear(); pending.clear()
        os.write(fd, data)
        await asyncio.get_running_loop().run_in_executor(None, fullsync)
        for w in waiters: w.set_result(None)
async def durable(rec):
    if MODE == "perwrite":
        os.write(fd, rec)
        await asyncio.get_running_loop().run_in_executor(None, fullsync)
    else:
        f = asyncio.get_running_loop().create_future()
        buf.append(rec); pending.append(f); await f
async def handle(r, w):
    try:
        while True:
            hdr = await r.readexactly(5)
            op, n = hdr[0], struct.unpack(">I", hdr[1:])[0]
            body = await r.readexactly(n)
            klen = struct.unpack(">H", body[:2])[0]
            key, val = body[2:2+klen], body[2+klen:]
            if op == 0:            # get
                v = store.get(key)
                resp = b"\x00" + (b"\x01" + v if v is not None else b"\x00")
            elif op == 1:          # put
                store[key] = val
                await durable(struct.pack(">BHI", 1, klen, len(val)) + key + val)
                resp = b"\x00"
            else:                  # delete
                store.pop(key, None)
                await durable(struct.pack(">BHI", 2, klen, 0) + key)
                resp = b"\x00"
            w.write(struct.pack(">I", len(resp)) + resp); await w.drain()
    except asyncio.IncompleteReadError:
        pass
async def main():
    if MODE == "group": asyncio.create_task(syncer())
    srv = await asyncio.start_server(handle, "127.0.0.1", PORT)
    async with srv: await srv.serve_forever()
asyncio.run(main())
