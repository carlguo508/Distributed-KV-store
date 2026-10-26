# Spike: are SQLite checkpoints behind the write tail latency?

Question: after kvbench gained a prefill (every key written before the timed run), one N=3 run
reported put p99 253 ms, against a 100 ms target, while Gets stayed below 1 ms. Is the cause
SQLite's WAL checkpoint, and does the prefill leave something behind that slows the timed run?

Answer, from the runs below: **checkpoints are a real but intermittent contributor; the prefill
leaves nothing behind.** SQLite auto-checkpoints inside the writer's COMMIT every time the WAL
reaches 1,000 pages, which happens every few seconds at 1,000 ops/s regardless of prefill.
Writes queued behind that COMMIT wait for it. Moving checkpoints to a background thread removes
the stall, but the WAL then grows without bound. **The sample is small (one or two runs per
case); repeat before relying on the numbers.**

Measured 2026-10-25, MacBook Pro M2 Pro, macOS 26.5.1, Python 3.14.6, N=3 on one machine,
1,000 ops/s, 100K keys, 1 KiB values, prefill on.

## Files

| File | Purpose |
|------|---------|
| `diag_server.py` | kvserver with the writer connection logging every transaction: commit time, whether it checkpointed (the main `kv.db` changed during COMMIT), WAL size. `BG_CKPT=<s>` adds a background PASSIVE checkpoint thread |
| `checkpoint.py` | Offline `PRAGMA wal_checkpoint(TRUNCATE)` on stopped databases |
| `run_test.sh` | One experiment with 3 diagnostic servers on ports 7400-7402 and fresh data (modes below) |
| `run_all.sh` | T1, T3, T4 three times each, interleaved |
| `analyze.py` | Joins kvbench `--per-second` output with the server logs: write p99 per second next to checkpoints in that second; max WAL size |
| `results/` | Per run: kvbench CSV, `.per-second.csv`, `.joined.csv`, `.summary.txt` |

Raw server logs and databases go to `data/` (git-ignored).

## Modes

| Mode | Procedure | Isolates |
|------|-----------|----------|
| T1 / BASE | Prefill, then the timed run immediately (60 s / 20 s) | Baseline |
| T3 | Prefill, stop, manual TRUNCATE checkpoint, restart, timed run | WAL left over from prefill |
| T4 | As T3, restarted with `wal_autocheckpoint=0` | No checkpoints at all during the timed run |
| BG | Prefill and timed run with `wal_autocheckpoint=0` on the writer and a PASSIVE checkpoint every 1 s on a second connection | Candidate fix |

```bash
spike/checkpoint/run_test.sh T1 1          # 60 s
SECS=20 spike/checkpoint/run_test.sh BG 1  # 20 s
```

## Results

| Run | Timed | Put p99 | Delete p99 | Get p99 | Write p99, seconds with checkpoint vs without (median) | Max WAL |
|-----|-------|---------|------------|---------|------------------------------------------|---------|
| T1-r1 | 60 s | 37.8 | 40.2 | 1.0 | 45.8 vs 28.9 | 4.2 MB |
| T1-r2 | 60 s | 44.3 | 37.5 | 1.4 | 40.4 vs 33.3 | 4.2 MB |
| T3-r1 | 60 s | 35.8 | 34.4 | 0.9 | 38.0 vs 31.4 | 4.2 MB |
| T4-r1 | 60 s | 29.8 | 27.8 | 1.0 | no checkpoints; 24.7 | 61-63 MB after 60 s |
| BASE-r1 | 20 s | **108.0** | **135.0** | 1.0 | 61.8 vs 31.8; 5 of 20 seconds over 100 ms | 4.3 MB |
| BG-r1 | 20 s | **32.3** | **32.5** | 0.7 | no second over 41 ms | **230-243 MB** |

All latencies in ms. WAL size is per server.

## Findings

1. **Prefill leaves nothing behind.** T3 starts the timed run with an empty WAL and behaves
   like T1. Commit times are flat across the timed run.
2. **Checkpoints add to the write tail.** Seconds containing a checkpoint have a higher write
   p99 in every run. Turning checkpoints off (T4) gives the lowest put p99.
3. **The failure is intermittent.** BASE-r1 reproduced the over-100 ms put p99; T1-r1 and r2
   did not. Its five worst seconds all contained a checkpoint, but some slow commits (up to
   237 ms) had none, so fsync stalls on the shared SSD also contribute.
4. **A background PASSIVE checkpoint fixes the tail but not the WAL.** Writer commits stayed at
   or under 31 ms. PASSIVE checkpoints block nobody (p50 about 95 ms, max 610 ms, off the write
   path), but the WAL resets only when a checkpoint completes with no write pending, which never
   happens under steady load. It reached about 230 MB during prefill and kept growing. An
   unbounded WAL costs disk space and makes restart recovery slower.

Not changed: `kvstore/storage.py`. Any fix belongs in a design decision first.
