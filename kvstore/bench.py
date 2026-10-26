"""kvbench: open-loop workload generator (requirement A5, design.md §8).

usage: python -m kvstore.bench --config cluster.json --rate 1000 --seconds 60 --out results/n3.csv
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field

import aiohttp

from .client import KVClient, WrongOwner
from .config import ClusterConfig, load_config

# requirement §2 Performance: p99 read < 50 ms, p99 write < 100 ms on a healthy system.
READ_P99_MS = 50.0
WRITE_P99_MS = 100.0
OPS = ("get", "put", "delete")
ERROR_KINDS = ("wrong_owner", "timeout", "connection", "other")
WRONG_OWNER_HINT = ("hint: got 421 WRONG_OWNER: the cluster file passed to kvbench does not match "
                    "the running servers (different server list, order or count)")


def classify_error(exc: BaseException) -> str:
    """Bucket a failed request so the report says why a run failed."""
    if isinstance(exc, WrongOwner):
        return "wrong_owner"
    # Timeout before connection: aiohttp.ServerTimeoutError subclasses both TimeoutError
    # and ClientConnectionError, and a timeout is the more useful label.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, aiohttp.ClientConnectionError):
        return "connection"
    return "other"


def pick_op(r: float) -> str:
    """Map a uniform random number in [0, 1) to the 80/15/5 mix (requirement A5)."""
    if r < 0.80:
        return "get"
    if r < 0.95:
        return "put"
    return "delete"


def percentile(sorted_samples: list[float], pct: float) -> float | None:
    """Nearest-rank percentile: the smallest sample with at least pct% of samples <= it.

    It always returns a real observed latency (no interpolation), so p99 is a request
    that actually happened. ceil, not round(): Python's round() rounds halves to even,
    which would make p99 of 100 samples pick rank 100 instead of 99.
    """
    if not sorted_samples:
        return None
    rank = math.ceil(pct * len(sorted_samples) / 100.0)
    return sorted_samples[min(len(sorted_samples), max(1, rank)) - 1]


@dataclass
class Report:
    latencies: dict[str, list[float]]
    seconds: float
    errors: int
    # Breakdown of `errors` by classify_error kind; only kinds that occurred are present.
    error_counts: dict[str, int] = field(default_factory=dict)
    # Timed Gets split by outcome: found a value vs NotFound. Shows the reads measured are
    # real lookups, not mostly misses.
    get_hits: int = 0
    get_not_found: int = 0
    # Keys written before the timed run (0 when prefill is off), and how long that took.
    prefill_keys: int = 0
    prefill_seconds: float = 0.0
    # One entry per timed request: (seconds after the run started, by SCHEDULED time; op;
    # latency in ms, or None if the request failed). Feeds the per-second table.
    timeline: list[tuple[float, str, float | None]] = field(default_factory=list)
    # Wall-clock time the timed run started, so per-second rows can be lined up with
    # timestamps from other sources such as server logs.
    started_at: float = 0.0
    rows: list[dict] = field(init=False)

    def __post_init__(self) -> None:
        # requirement A5: p50/p95/p99/p999 per operation.
        self.rows = []
        for op in OPS:
            s = sorted(self.latencies.get(op, []))
            self.rows.append({
                "op": op, "count": len(s), "ops_per_sec": len(s) / self.seconds if self.seconds else 0.0,
                "p50_ms": percentile(s, 50), "p95_ms": percentile(s, 95), "p99_ms": percentile(s, 99),
                "p999_ms": percentile(s, 99.9), "max_ms": s[-1] if s else None,
                # Hit counts only apply to Gets; None renders as an empty CSV cell.
                "hits": self.get_hits if op == "get" else None,
                "not_found": self.get_not_found if op == "get" else None,
            })

    def p99(self, op: str) -> float | None:
        return next(r["p99_ms"] for r in self.rows if r["op"] == op)

    def passed(self) -> bool:
        # Any error fails the run: a fast system that drops requests is not "healthy".
        if self.errors:
            return False
        # Put and Delete are both writes, so each must meet the write target on its own.
        read_ok = (self.p99("get") or 0.0) < READ_P99_MS
        write_ok = all((self.p99(op) or 0.0) < WRITE_P99_MS for op in ("put", "delete"))
        return read_ok and write_ok

    def to_csv(self) -> str:
        cols = ["op", "count", "ops_per_sec", "p50_ms", "p95_ms", "p99_ms", "p999_ms", "max_ms",
                "hits", "not_found"]
        fmt = lambda v: "" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
        return "\n".join([",".join(cols)] + [",".join(fmt(r[c]) for c in cols) for r in self.rows]) + "\n"

    def per_second(self) -> list[dict]:
        """Latency per second of the timed run, so short stalls (for example a checkpoint inside
        a COMMIT) show up as slow seconds instead of disappearing into whole-run percentiles."""
        buckets: dict[int, list[tuple[str, float | None]]] = {}
        for t, op, ms in self.timeline:
            buckets.setdefault(int(t), []).append((op, ms))
        rows = []
        for sec in sorted(buckets):
            items = buckets[sec]
            writes = sorted(ms for op, ms in items if op != "get" and ms is not None)
            gets = sorted(ms for op, ms in items if op == "get" and ms is not None)
            rows.append({
                "t": sec, "epoch": self.started_at + sec, "writes": len(writes),
                "w_p50": percentile(writes, 50), "w_p99": percentile(writes, 99),
                "w_max": writes[-1] if writes else None,
                "errors": sum(1 for _, ms in items if ms is None),
                # Get p99 alongside: if reads are slow too, the whole machine stalled, not
                # just the write path (reads never touch SQLite).
                "g_p99": percentile(gets, 99),
            })
        return rows

    def per_second_text(self) -> str:
        f = lambda v: "-" if v is None else f"{v:7.1f}"
        lines = [f"{'t':>4s}{'writes':>8s}{'w_p50':>8s}{'w_p99':>8s}{'w_max':>8s}{'errs':>6s}{'g_p99':>8s}"]
        for r in self.per_second():
            lines.append(f"{r['t']:4d}{r['writes']:8d} {f(r['w_p50'])} {f(r['w_p99'])} {f(r['w_max'])}"
                         f"{r['errors']:6d} {f(r['g_p99'])}")
        return "\n".join(lines)

    def per_second_csv(self) -> str:
        fmt = lambda v: "" if v is None else f"{v:.2f}"
        lines = ["t,epoch,writes,w_p50_ms,w_p99_ms,w_max_ms,errors,g_p99_ms"]
        for r in self.per_second():
            lines.append(f"{r['t']},{r['epoch']:.3f},{r['writes']},{fmt(r['w_p50'])},{fmt(r['w_p99'])},"
                         f"{fmt(r['w_max'])},{r['errors']},{fmt(r['g_p99'])}")
        return "\n".join(lines) + "\n"

    def to_text(self) -> str:
        total = sum(r["count"] for r in self.rows)
        lines = []
        if self.prefill_keys:
            lines.append(f"prefill: {self.prefill_keys} keys in {self.prefill_seconds:.1f}s (not measured)")
        lines += [f"{'op':7s}{'count':>8s}{'ops/s':>9s}{'p50':>9s}{'p95':>9s}{'p99':>9s}{'p99.9':>9s}{'max':>9s}"]
        for r in self.rows:
            f = lambda v: "-" if v is None else f"{v:8.2f}"
            lines.append(f"{r['op']:7s}{r['count']:8d}{r['ops_per_sec']:9.0f}{f(r['p50_ms'])}{f(r['p95_ms'])}"
                         f"{f(r['p99_ms'])}{f(r['p999_ms'])}{f(r['max_ms'])}")
        gets = self.get_hits + self.get_not_found
        if gets:
            lines.append(f"get: hits {self.get_hits} ({100.0 * self.get_hits / gets:.1f}%), "
                         f"not found {self.get_not_found} ({100.0 * self.get_not_found / gets:.1f}%)")
        lines.append(f"total {total} ops in {self.seconds:.0f}s = {total / self.seconds:.0f} ops/s, errors={self.errors}")
        if self.error_counts:
            lines.append("errors by type: " + ", ".join(
                f"{k}={self.error_counts[k]}" for k in ERROR_KINDS if k in self.error_counts))
        if self.error_counts.get("wrong_owner"):
            lines.append(WRONG_OWNER_HINT)
        lines.append(f"targets: read p99 < {READ_P99_MS:.0f} ms, write p99 < {WRITE_P99_MS:.0f} ms -> "
                     + ("PASS" if self.passed() else "FAIL"))
        return "\n".join(lines)


async def run(cfg: ClusterConfig, rate: int, seconds: float, n_keys: int = 100_000,
              value_size: int = 1024, conns_per_server: int = 32, seed: int | None = None,
              prefill: bool = True) -> Report:
    rng = random.Random(seed)
    keys = [f"key-{i:08d}".encode() for i in range(n_keys)]          # uniform key space (A5)
    # One random value reused for every Put: generating 1 KiB per request would cost
    # generator CPU and add noise to the latency being measured.
    value = os.urandom(value_size)
    latencies: dict[str, list[float]] = {op: [] for op in OPS}
    errors = 0
    error_counts: dict[str, int] = {}
    get_hits = get_not_found = 0
    timeline: list[tuple[float, str, float | None]] = []
    start = 0.0                                   # set when the timed run begins
    q: asyncio.Queue = asyncio.Queue()

    def count_error(exc: BaseException) -> None:
        nonlocal errors
        errors += 1
        kind = classify_error(exc)
        error_counts[kind] = error_counts.get(kind, 0) + 1

    async def prefill_worker(kv: KVClient, it) -> None:
        # Workers share one iterator, so each key is written exactly once. Prefill errors are
        # counted like timed ones: a failed prefill (e.g. a wrong cluster file) fails the run.
        for key in it:
            try:
                await kv.put(key, value)
            except Exception as exc:  # noqa: BLE001 - counted, prefill continues
                count_error(exc)

    async def worker(kv: KVClient) -> None:
        nonlocal get_hits, get_not_found
        while True:
            item = await q.get()
            if item is None:
                return
            op, scheduled_at = item
            key = keys[rng.randrange(n_keys)]
            try:
                if op == "get":
                    if await kv.get(key) is None:
                        get_not_found += 1
                    else:
                        get_hits += 1
                elif op == "put":
                    await kv.put(key, value)
                else:
                    await kv.delete(key)
                # Latency is measured from the request's SCHEDULED time, not from when it was
                # enqueued or picked up. If the generator or the system falls behind, that
                # delay counts, which avoids "coordinated omission" hiding slow periods.
                ms = (time.perf_counter() - scheduled_at) * 1000.0
                latencies[op].append(ms)
                timeline.append((scheduled_at - start, op, ms))
            except Exception as exc:  # noqa: BLE001 - counted, run continues
                count_error(exc)
                timeline.append((scheduled_at - start, op, None))

    async with KVClient(cfg, conns_per_server=conns_per_server) as kv:
        # Prefill: write every key once before the clock starts. Without it the store starts
        # empty and ~all Gets are NotFound, which measures a cheap miss instead of a real read
        # of a 1 KiB value. Closed loop, as fast as the pool allows; it is setup, not measured.
        prefill_start = time.perf_counter()
        if prefill:
            it = iter(keys)
            await asyncio.gather(*(prefill_worker(kv, it) for _ in range(conns_per_server * cfg.n)))
        prefill_seconds = time.perf_counter() - prefill_start

        # One worker per pooled connection, so each worker holds at most one in-flight request.
        workers = [asyncio.create_task(worker(kv)) for _ in range(conns_per_server * cfg.n)]
        interval = 1.0 / rate
        started_at = time.time()
        start = time.perf_counter()
        next_at, end = start, start + seconds
        # Open loop (requirement A5): requests are issued on a fixed schedule whether or not
        # earlier ones finished, like independent real clients. A closed loop would slow down
        # when the server does and under-report tail latency. Sleeping until an absolute
        # deadline (next_at) instead of a fixed interval keeps the rate from drifting.
        # Each request is stamped with next_at, its slot in the schedule. If the event loop
        # stalls (GC pause, a slow callback), the requests whose slots passed are enqueued late
        # in a burst, and their latency still starts at the slot, not at the late enqueue.
        while next_at < end:
            q.put_nowait((pick_op(rng.random()), next_at))
            next_at += interval
            delay = next_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        for _ in workers:
            q.put_nowait(None)
        await asyncio.gather(*workers)
        elapsed = time.perf_counter() - start
    return Report(latencies=latencies, seconds=elapsed, errors=errors, error_counts=error_counts,
                  get_hits=get_hits, get_not_found=get_not_found,
                  prefill_keys=n_keys if prefill else 0, prefill_seconds=prefill_seconds if prefill else 0.0,
                  timeline=timeline, started_at=started_at)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kvbench")
    p.add_argument("--config", default="cluster.json")
    p.add_argument("--rate", type=int, default=1000, help="target ops/s (open loop)")
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--keys", type=int, default=100_000)
    p.add_argument("--value-size", type=int, default=1024)
    p.add_argument("--conns", type=int, default=32, help="keep-alive connections per server")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--out", default=None, help="CSV path")
    p.add_argument("--no-prefill", action="store_true",
                   help="skip writing every key before the timed run")
    p.add_argument("--per-second", action="store_true",
                   help="print write latency per second; with --out also save <out>.per-second.csv")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    print(f"kvbench: {cfg.n} server(s), {a.rate} ops/s for {a.seconds:.0f}s, 80/15/5, "
          f"{a.keys} keys, {a.value_size} B values, {a.conns} conns/server, "
          f"prefill {'off' if a.no_prefill else 'on'}", file=sys.stderr, flush=True)
    rep = asyncio.run(run(cfg, a.rate, a.seconds, a.keys, a.value_size, a.conns, a.seed,
                          prefill=not a.no_prefill))
    if a.per_second:
        print(rep.per_second_text())
    print(rep.to_text())
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w") as f:
            f.write(rep.to_csv())
        print(f"csv written to {a.out}")
        if a.per_second:
            ps_path = os.path.splitext(a.out)[0] + ".per-second.csv"
            with open(ps_path, "w") as f:
                f.write(rep.per_second_csv())
            print(f"per-second csv written to {ps_path}")
    # Exit code reflects pass/fail so scripts and CI can gate on it.
    return 0 if rep.passed() else 1


if __name__ == "__main__":
    sys.exit(main())
