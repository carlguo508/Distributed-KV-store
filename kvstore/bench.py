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
        cols = ["op", "count", "ops_per_sec", "p50_ms", "p95_ms", "p99_ms", "p999_ms", "max_ms"]
        fmt = lambda v: "" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
        return "\n".join([",".join(cols)] + [",".join(fmt(r[c]) for c in cols) for r in self.rows]) + "\n"

    def to_text(self) -> str:
        total = sum(r["count"] for r in self.rows)
        lines = [f"{'op':7s}{'count':>8s}{'ops/s':>9s}{'p50':>9s}{'p95':>9s}{'p99':>9s}{'p99.9':>9s}{'max':>9s}"]
        for r in self.rows:
            f = lambda v: "-" if v is None else f"{v:8.2f}"
            lines.append(f"{r['op']:7s}{r['count']:8d}{r['ops_per_sec']:9.0f}{f(r['p50_ms'])}{f(r['p95_ms'])}"
                         f"{f(r['p99_ms'])}{f(r['p999_ms'])}{f(r['max_ms'])}")
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
              value_size: int = 1024, conns_per_server: int = 32, seed: int | None = None) -> Report:
    rng = random.Random(seed)
    keys = [f"key-{i:08d}".encode() for i in range(n_keys)]          # uniform key space (A5)
    # One random value reused for every Put: generating 1 KiB per request would cost
    # generator CPU and add noise to the latency being measured.
    value = os.urandom(value_size)
    latencies: dict[str, list[float]] = {op: [] for op in OPS}
    errors = 0
    error_counts: dict[str, int] = {}
    q: asyncio.Queue = asyncio.Queue()

    async def worker(kv: KVClient) -> None:
        nonlocal errors
        while True:
            item = await q.get()
            if item is None:
                return
            op, scheduled_at = item
            key = keys[rng.randrange(n_keys)]
            try:
                if op == "get":
                    await kv.get(key)
                elif op == "put":
                    await kv.put(key, value)
                else:
                    await kv.delete(key)
                # Latency is measured from the request's SCHEDULED time, not from when it was
                # enqueued or picked up. If the generator or the system falls behind, that
                # delay counts, which avoids "coordinated omission" hiding slow periods.
                latencies[op].append((time.perf_counter() - scheduled_at) * 1000.0)
            except Exception as exc:  # noqa: BLE001 - counted, run continues
                errors += 1
                kind = classify_error(exc)
                error_counts[kind] = error_counts.get(kind, 0) + 1

    async with KVClient(cfg, conns_per_server=conns_per_server) as kv:
        # One worker per pooled connection, so each worker holds at most one in-flight request.
        workers = [asyncio.create_task(worker(kv)) for _ in range(conns_per_server * cfg.n)]
        interval = 1.0 / rate
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
    return Report(latencies=latencies, seconds=elapsed, errors=errors, error_counts=error_counts)


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
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    print(f"kvbench: {cfg.n} server(s), {a.rate} ops/s for {a.seconds:.0f}s, 80/15/5, "
          f"{a.keys} keys, {a.value_size} B values, {a.conns} conns/server", file=sys.stderr, flush=True)
    rep = asyncio.run(run(cfg, a.rate, a.seconds, a.keys, a.value_size, a.conns, a.seed))
    print(rep.to_text())
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w") as f:
            f.write(rep.to_csv())
        print(f"csv written to {a.out}")
    # Exit code reflects pass/fail so scripts and CI can gate on it.
    return 0 if rep.passed() else 1


if __name__ == "__main__":
    sys.exit(main())
