# Spike: are SQLite checkpoints behind the write tail latency?

Question: after kvbench gained a prefill (every key written before the timed run), one N=3 run
reported put p99 253 ms, against a 100 ms target, while Gets stayed below 1 ms. Is the cause
SQLite's WAL checkpoint, and does the prefill leave something behind that slows the timed run?

Answer, from the runs below: **checkpoints are a real but intermittent contributor; the prefill
leaves nothing behind.** SQLite auto-checkpoints inside the writer's COMMIT every time the WAL
reaches 1,000 pages, which happens every few seconds at 1,000 ops/s regardless of prefill.
Writes queued behind that COMMIT wait for it. Moving checkpoints to a background thread removes
the stall, but the WAL then grows without bound. The fix that went into `kvstore/storage.py`
(commit 1a729a9) keeps the background checkpoint and bounds the WAL with a TRUNCATE on the
writer above 64 MB. With the fix, both the 20 s runs (FIX, three runs) and the latest 60 s
re-measurement on the current code (2026-10-26, `results/n3.csv` at the repo root) pass, with
no second over 100 ms.

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
| FIX / FIX60 | Prefill and timed run (20 s / 60 s) on unmodified `kvstore.storage` after the fix: background PASSIVE every 1 s, writer TRUNCATE between batches when the WAL passes 64 MB | Shipped fix |

```bash
spike/checkpoint/run_test.sh T1 1          # 60 s
SECS=20 spike/checkpoint/run_test.sh BG 1  # 20 s
SECS=20 spike/checkpoint/run_test.sh FIX 1 # 20 s
spike/checkpoint/run_test.sh FIX60 1       # 60 s
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

After the fix (writer never checkpoints inside COMMIT; the column below is per-second write p99
across all seconds, since no second contains a COMMIT checkpoint):

| Run | Timed | Put p99 | Delete p99 | Get p99 | Write p99 per second, median / worst | Seconds over 100 ms | Max WAL, whole run / timed |
|-----|-------|---------|------------|---------|--------------------------------------|---------------------|----------------------------|
| FIX-r1 | 20 s | 32.6 | 33.7 | 0.8 | 31.3 / 38.8 | 0/20 | 67 MB / 21-28 MB |
| FIX-r2 | 20 s | 31.5 | 33.5 | 1.0 | 30.6 / 43.4 | 0/20 | 67-68 MB / 24-38 MB |
| FIX-r3 | 20 s | 30.8 | 32.1 | 0.8 | 29.4 / 41.1 | 0/20 | 68 MB / 19-34 MB |
| Latest, N=3 (`results/n3.csv`, 2026-10-26) | 60 s | 30.9 | 31.7 | 0.7 | 29.7 / 36.0 | 0/60 | not logged |
| Latest, N=1 (`results/n1.csv`, 2026-10-26) | 60 s | 14.5 | 14.0 | 1.0 | 12.6 / 56.5 | 0/60 | not logged |

All latencies in ms. WAL size is per server. Whole-run WAL peaks at about 67 MB because the
prefill drives it to the 64 MB limit before the writer truncates. The timed-run WAL never got
past 38 MB, so every writer TRUNCATE happened during the prefill, none in the timed run. The
latest runs use the plain `kvstore.server` via `scripts/start.sh` and `kvstore.bench
--per-second`, so they have per-second latency but no commit or WAL log.

Superseded: the FIX60 runs from the night of the fix (`results/FIX60-r*`) are kept for the
record but no longer describe the current code; the 2026-10-26 re-measurement replaces them.

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
5. **The shipped fix removes the checkpoint stall and bounds the WAL.** Writer commits in the
   timed run stay at p99 17-26 ms with no checkpoint inside them. The WAL stays under the
   64 MB limit plus one batch (about 67 MB). The writer TRUNCATE is slow (worst per server
   80-820 ms) but runs between batches, 3-16 times per server per run, and in every run all of
   them fell in the prefill.
6. **The fix holds over 60 s.** The latest 60 s runs on the current code pass on both N=3
   (put p99 30.9 ms, worst second 36 ms) and N=1 (put p99 14.5 ms, worst second 57 ms), with
   0 errors and Get p99 under 6 ms in every second.
