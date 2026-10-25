"""Open-loop HTTP workload generator. Routes each key by md5 to its owning server.
usage: python http_client.py <port[,port,...]> <ops_per_sec> <seconds>
"""
import asyncio, base64, hashlib, os, random, sys, time
import aiohttp
PORTS = [int(p) for p in sys.argv[1].split(",")]
RATE, SECS, CONNS = int(sys.argv[2]), int(sys.argv[3]), 32
lat = {"get": [], "put": [], "del": []}; status_errors = 0
random.seed(1); keys = [os.urandom(16).hex().encode() for _ in range(100_000)]
urls = {k: f"http://127.0.0.1:{PORTS[int(hashlib.md5(k).hexdigest(), 16) % len(PORTS)]}/keys/"
           + base64.urlsafe_b64encode(k).decode().rstrip("=") for k in keys}
val = os.urandom(1024)
async def worker(q, sess):
    global status_errors
    while True:
        item = await q.get()
        if item is None: break
        op, start = item
        key = keys[min(int(random.paretovariate(1.2)) % len(keys), len(keys)-1)]
        m = ("GET", "PUT", "DELETE")[op]
        async with sess.request(m, urls[key], data=val if op == 1 else None) as r:
            await r.read()
            if r.status not in (200, 404): status_errors += 1
        lat[("get","put","del")[op]].append((time.perf_counter() - start) * 1000)
async def main():
    conn = aiohttp.TCPConnector(limit=0, limit_per_host=CONNS)
    async with aiohttp.ClientSession(connector=conn) as sess:
        q = asyncio.Queue(); ws = [asyncio.create_task(worker(q, sess)) for _ in range(CONNS)]
        interval = 1 / RATE; t = time.perf_counter(); end = t + SECS
        while t < end:
            r = random.random(); op = 0 if r < .8 else (1 if r < .95 else 2)
            q.put_nowait((op, time.perf_counter()))
            t += interval; d = t - time.perf_counter()
            if d > 0: await asyncio.sleep(d)
        for _ in ws: q.put_nowait(None)
        await asyncio.gather(*ws)
    for k, v in lat.items():
        v.sort(); n = len(v)
        if n: print(f"{k:4s} n={n:6d} p50={v[n//2]:.2f} p99={v[int(n*.99)]:.2f} max={v[-1]:.2f} ms")
    tot = sum(len(v) for v in lat.values())
    print(f"achieved {tot/SECS:.0f} ops/s across {len(PORTS)} server(s), unexpected statuses: {status_errors}")
asyncio.run(main())
