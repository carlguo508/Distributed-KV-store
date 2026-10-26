"""Join kvbench's per-second table with the servers' commit logs for one run.

usage: analyze.py <spike/checkpoint dir> <run name, e.g. T1-r1>

Reads   results/<name>.csv, results/<name>.per-second.csv, data/<name>/s*-timed.commits.csv
Writes  results/<name>.joined.csv: per second, the client write p99 next to the number of
        checkpoint commits and the slowest commit across all servers in that second.
Prints  a summary: whole-run write p99, slow seconds, write p99 in seconds with vs without a
        checkpoint, and per-server commit times with vs without a checkpoint.
"""
import csv
import glob
import math
import sys
from collections import defaultdict

here, name = sys.argv[1], sys.argv[2]


def pct(v, q):
    if not v:
        return None
    v = sorted(v)
    return v[min(len(v), max(1, math.ceil(q * len(v) / 100))) - 1]


def f(v):
    return "-" if v is None else f"{v:.1f}"


ops = {r["op"]: r for r in csv.DictReader(open(f"{here}/results/{name}.csv"))}
secs = list(csv.DictReader(open(f"{here}/results/{name}.per-second.csv")))
t0 = float(secs[0]["epoch"]) - int(secs[0]["t"])
t_end = t0 + int(secs[-1]["t"]) + 1

commits = []  # (second index, commit_ms, is_checkpoint, server)
for path in sorted(glob.glob(f"{here}/data/{name}/s*-timed.commits.csv")):
    server = path.rsplit("/", 1)[1].split("-")[0]
    rows = list(csv.DictReader(open(path)))
    # With the storage fix, COMMIT never checkpoints, and the kv.db-changed flag on a commit only
    # means the background thread wrote kv.db at the same time, so it is ignored.
    fixed = any(r.get("kind") in ("writer_truncate", "bg_passive") for r in rows)
    for r in rows:
        t = float(r["t"])
        kind = r.get("kind") or "commit"   # logs before the storage checkpoint fix have no kind
        if t0 <= t < t_end and kind == "commit":
            commits.append((int(t - t0), float(r["commit_ms"]), r["ckpt"] == "1" and not fixed, server))
        elif t0 <= t < t_end and kind == "writer_truncate":
            # A TRUNCATE checkpoint on the writer blocks the next batch, like a checkpoint
            # inside COMMIT did before the fix, so it counts as a checkpoint stall.
            commits.append((int(t - t0), float(r["commit_ms"]), True, server))

ck_count, slowest = defaultdict(int), defaultdict(float)
for s, ms, ck, _ in commits:
    ck_count[s] += ck
    slowest[s] = max(slowest[s], ms)

with open(f"{here}/results/{name}.joined.csv", "w") as out:
    out.write("t,writes,w_p99_ms,g_p99_ms,checkpoints,slowest_commit_ms\n")
    for r in secs:
        s = int(r["t"])
        out.write(f"{s},{r['writes']},{r['w_p99_ms']},{r['g_p99_ms']},{ck_count[s]},{slowest[s]:.1f}\n")

w99 = {int(r["t"]): float(r["w_p99_ms"]) for r in secs if r["w_p99_ms"]}
with_ck = [v for s, v in w99.items() if ck_count[s]]
no_ck = [v for s, v in w99.items() if not ck_count[s]]
print(f"[{name}] whole run: put p99={ops['put']['p99_ms']} delete p99={ops['delete']['p99_ms']} "
      f"get p99={ops['get']['p99_ms']} ms; errors={sum(int(r['errors']) for r in secs)}")
print(f"  per-second write p99: median={f(pct(list(w99.values()), 50))} worst={f(max(w99.values()))} ms; "
      f"seconds over 100 ms: {sum(v > 100 for v in w99.values())}/{len(w99)}")
print(f"  seconds WITH a checkpoint: {len(with_ck)}, median of their write p99={f(pct(with_ck, 50))} ms | "
      f"WITHOUT: {len(no_ck)}, median={f(pct(no_ck, 50))} ms")
worst = sorted(w99, key=lambda s: -w99[s])[:5]
print("  worst seconds: " + "; ".join(
    f"t={s} p99={w99[s]:.0f} ckpts={ck_count[s]} slowest commit={slowest[s]:.0f}" for s in worst))
for path in sorted(glob.glob(f"{here}/data/{name}/s*-timed.commits.csv")):
    server = path.rsplit("/", 1)[1].split("-")[0]
    rows = list(csv.DictReader(open(path)))
    wal_all = max(int(r["wal_bytes"]) for r in rows)
    wal_timed = max((int(r["wal_bytes"]) for r in rows if t0 <= float(r["t"]) < t_end), default=0)
    line = f"  {server}: max WAL whole run (incl. prefill)={wal_all / 1e6:.1f} MB, timed run={wal_timed / 1e6:.1f} MB"
    tr = [float(r["commit_ms"]) for r in rows if r.get("kind") == "writer_truncate"]
    bp = [float(r["commit_ms"]) for r in rows if r.get("kind") == "bg_passive"]
    tr_timed = [(int(float(r["t"]) - t0), float(r["commit_ms"])) for r in rows
                if r.get("kind") == "writer_truncate" and t0 <= float(r["t"]) < t_end]
    if tr or bp:
        line += (f"; writer TRUNCATEs={len(tr)} (in timed run: {len(tr_timed)}"
                 + "".join(f", t={s} {ms:.0f} ms" for s, ms in tr_timed) + ")"
                 + f" max={f(max(tr) if tr else None)} ms; "
                 f"background PASSIVE={len(bp)} p50={f(pct(bp, 50))} max={f(max(bp) if bp else None)} ms")
    bgp = path.replace(".commits.csv", ".bgckpt.csv")
    if glob.glob(bgp):
        b = list(csv.DictReader(open(bgp)))
        bms = [float(r["ckpt_ms"]) for r in b]
        line += (f"; background checkpoints={len(b)} p50={f(pct(bms, 50))} max={f(max(bms))} ms, "
                 f"busy={sum(int(r['busy']) for r in b)}")
    print(line)
for server in sorted({c[3] for c in commits}):
    cs = [c for c in commits if c[3] == server]
    ck = [c[1] for c in cs if c[2]]
    nk = [c[1] for c in cs if not c[2]]
    print(f"  {server}: commits={len(cs)} plain p50={f(pct(nk, 50))} p99={f(pct(nk, 99))} max={f(max(nk))} | "
          f"checkpoints={len(ck)} p50={f(pct(ck, 50))} max={f(max(ck) if ck else None)} ms")
