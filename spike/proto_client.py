"""Open-loop workload generator for the spike. Routes each key to one server by hash.
usage: python3 proto_client.py <port[,port,...]> <ops_per_sec> <seconds>
"""
import asyncio, hashlib, os, random, struct, sys, time
PORTS = [int(p) for p in sys.argv[1].split(",")]
RATE = int(sys.argv[2]); SECS = int(sys.argv[3]); CONNS = 16
lat = {"get": [], "put": [], "del": []}
random.seed(1); keys = [os.urandom(16).hex().encode() for _ in range(100_000)]
owner = {k: int(hashlib.md5(k).hexdigest(), 16) % len(PORTS) for k in keys}
val = os.urandom(1024)
async def worker(q):
    conns = [await asyncio.open_connection("127.0.0.1", p) for p in PORTS]
    while True:
        item = await q.get()
        if item is None: break
        op, start = item
        key = keys[min(int(random.paretovariate(1.2)) % len(keys), len(keys)-1)]
        r, w = conns[owner[key]]
        body = struct.pack(">H", len(key)) + key + (val if op == 1 else b"")
        w.write(bytes([op]) + struct.pack(">I", len(body)) + body); await w.drain()
        n = struct.unpack(">I", await r.readexactly(4))[0]; await r.readexactly(n)
        lat[("get","put","del")[op]].append((time.perf_counter() - start) * 1000)
    for _, w in conns: w.close()
async def main():
    q = asyncio.Queue(); ws = [asyncio.create_task(worker(q)) for _ in range(CONNS)]
    interval = 1 / RATE; t = time.perf_counter(); end = t + SECS
    while t < end:
        r = random.random(); op = 0 if r < .8 else (1 if r < .95 else 2)
        q.put_nowait((op, time.perf_counter()))   # open-loop: latency includes queueing
        t += interval; d = t - time.perf_counter()
        if d > 0: await asyncio.sleep(d)
    for _ in ws: q.put_nowait(None)
    await asyncio.gather(*ws)
    for k, v in lat.items():
        v.sort(); n = len(v)
        if n: print(f"{k:4s} n={n:6d} p50={v[n//2]:.2f} p99={v[int(n*.99)]:.2f} max={v[-1]:.2f} ms")
    tot = sum(len(v) for v in lat.values()); print(f"achieved {tot/SECS:.0f} ops/s across {len(PORTS)} server(s)")
asyncio.run(main())
