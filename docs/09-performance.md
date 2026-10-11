# Running on the game PC without costing frames

The backend will share a machine with F1 26. This document is the answer to "how do we
make it light", and the first thing it says is that *light* has to be defined as a
measurement, not a feeling.

## What actually causes stutter

Plan v1 assumed garbage collection. It cannot be: the backend is a separate OS process,
so its GC pauses its own threads, not the game's render loop. The real contention
channels, roughly in order of how much they matter:

1. **CPU core contention.** The game wants its render and simulation threads scheduled
   without delay. A background process that occasionally burns a core — or worse, gets
   scheduled onto the core the render thread is on — shows up as 1 % low frame times.
2. **GPU contention.** A browser rendering the dashboard on the same GPU is far more
   expensive than the Python backend. This is the single biggest risk on a co-hosted
   setup; the second-monitor page must therefore be cheap (see *Client* below).
3. **Disk I/O spikes.** A recorder that flushes synchronously, or SQLite committing on
   every row, produces stalls that can reach the game if they hit the same drive.
4. **Memory pressure.** Only if the machine is already near its limit; F1 26 plus a
   browser plus a 150 MB Python process is not a problem on 16 GB, but a 2 GB in-memory
   history would be.
5. **Timer resolution.** Some processes raise the global Windows timer resolution, which
   affects scheduling machine-wide. We must not do this.

None of these is fixed by allocating less in the parse loop. All of them are fixed by
process-level hygiene, which is cheaper and more effective.

## Budget

| Metric | Target | How measured |
|---|---|---|
| Backend CPU | < 2 % of total on an 8-core machine, steady state | Windows perf counters, logged per session |
| Backend RSS | < 150 MB steady, < 250 MB peak | same |
| Disk write | < 2 MB/s average (expected ~0.25 MB/s at 30 Hz), no single flush > 8 MB | recorder counters |
| Game 1 % low frame time | within noise of a no-backend baseline | PresentMon / CapFrameX, A/B |
| Tick overrun (10 Hz) | p99 < 20 ms of the 100 ms budget | internal histogram |

The fourth row is the one that answers the user's question, and it is the only one that
can settle it. Procedure: drive the same replayable benchmark (a hotlap on the same
track, same settings) three times with the backend off and three times with it on,
capture frame times, and compare 1 % and 0.1 % lows. Do this once at M1 and once at M3,
and put the numbers in the repo. If the difference is inside run-to-run noise, the
question is closed and we stop paying for micro-optimisations.

## Measures we take by default

**Process placement**
- Run the backend at **below-normal priority**. The game then wins every scheduling
  contest, and our worst case is a few ms of added call latency, which the budget has
  room for.
- Optionally pin the backend to the last one or two logical cores (config:
  `cpu_affinity`). Whether this helps or hurts depends on the CPU — measure before
  enabling; on CPUs with efficiency cores, pinning to those is usually the right answer.
- Never touch the global timer resolution.

**Network**
- When co-hosted, set the game's UDP IP to **`127.0.0.1`** with broadcast off. Loopback
  skips the NIC driver entirely and removes the LAN as a failure mode. The phone still
  reaches the dashboard on the PC's LAN address over TCP — the two paths are independent.
- A single socket with a large receive buffer (`SO_RCVBUF` ~4 MB) so a brief stall never
  drops packets; count and expose drops.

**Hot path**
- One thread does socket read → parse → state update. No locks on it.
- `struct.Struct.unpack_from` into preallocated `__slots__` objects; no per-packet dicts
  or lists; reuse a `bytearray` receive buffer.
- `gc.freeze()` after warm-up, and automatic GC disabled during green-flag running with
  an explicit `gc.collect()` at safe moments (in the garage, during a pause, between
  sessions). This is the one place where the v1 instinct was right; it just needed a
  different justification and a much smaller scope.
- Not all packets need full parsing every frame. Motion at 60 Hz is only used for
  context — parse it at a decimated rate, or skip it entirely unless a rule needs it.
  Cheapest optimisation available: **lower the in-game send rate to 20–30 Hz**. The only
  thing that genuinely benefits from 60 Hz is thermal smoothing, and the slow EMA does
  not care. Make the rate a documented setting and default the recommendation to 30 Hz.

**I/O off the hot path**
- Recorder: append to a buffered writer on a dedicated thread fed by a bounded queue,
  flushed in ~1 MB chunks, never `fsync`. On queue overflow, drop recording rows and
  raise a UI badge — recording must never back-pressure ingest.
- SQLite: WAL mode, `synchronous=NORMAL`, one background writer thread, batched at lap
  boundaries rather than per row.
- Recommend a different physical drive from the game install where one exists, and a
  Defender exclusion for the recording directory (real-time AV scanning of a file being
  appended continuously is a genuine cost).
- Compress finished recordings (zstd) in a below-normal-priority job after the session
  closes, never during running.

**Client**
- The dashboard is on a **second monitor** driven by the game PC (your setup), so it
  shares the game's GPU. Keep that page cheap: static text at a 5 Hz state rate, no
  canvas or charts in race mode, no animations, gradients or shadows,
  `content-visibility`, and a window that is not fullscreen-composited. Hardware
  acceleration on for that browser; measure it in the frame-time A/B, since it is likely
  to cost more than the backend does.
- State at 5–10 Hz, not 60 Hz; deltas rather than full snapshots where it is easy.

## TTS synthesis latency

`trigger_to_speak_ms` in `src/pitwall/metrics.py` is the voice lag. The dashboard shows it as
"voice lag" p99. Synthesis is the part of that lag that changes with the speech engine.
`scripts/perf_tts.py` measures it for one lap of radio calls.

The workload is 22 fixed lines in the script. Twenty are P2 and P3 calls, which is the top
`budget_steps` step in `menu.yaml` (20 calls per lap). Two are P1 calls. The budget does not cap P1
calls, so they come on top of the 20. Each line uses the speed and expression of its priority tone.
Fifteen lines are fixed phrases from the rule YAML. Seven are rule templates with example values,
because gap, fuel and plan calls change every lap and never hit the cache.

The script reads the `speech` settings and changes nothing. For each line it reports these values:

- `cold_ms`: time for the synth of that priority on uncached text, median of `--repeat` passes
  (default 3)
- `audio_s`: length of the speech it produced
- `rtf`: `cold_ms` divided by `audio_s`
- `warm`: whether `pitwall voice warm` pre-renders the line (`common_phrases()`)

The summary block lists the engine, voice, sample rate and model load time. It also lists the lap
totals and the p50 and p99 for each priority. The script then renders the lap twice through
`PiperSpeaker.render()`. The first pass is cold and the second pass hits the in-memory cache.

If the backend can't load, the script prints the reason and exits with code 2. A Piper voice that is
not downloaded causes this, and so do missing Kokoro model files or a missing `kokoro-onnx`.

### Compare Piper and Kokoro

Run the same script with the same settings on both checkouts:

1. On `main`, run `uv run pitwall voice get`. Then run `uv run python scripts/perf_tts.py`.
2. Add a worktree for the Kokoro branch:
   `git worktree add ../f1-pitwall-kokoro origin/devin/1790482416-kokoro-tts`.
3. If the branch has no `scripts/perf_tts.py`, copy the script from `main` into the worktree.
4. In the worktree, run `uv sync`. Then run `uv run pitwall voices kokoro`.
5. In the worktree, run `uv run python scripts/perf_tts.py`.
6. Compare the p50 and p99 for each priority, `lap_cold_ms` and `budget_cold_ms`.
   Report both the cold and the warm figures.

`--engine`, `--voice` and `--speed` override the settings, as in `pitwall voice say`.

The cold figures are an upper bound for a real session. Repeated phrases come from the cache in
about 0 ms, and on `main` `pitwall voice warm` stores the rendered P1 phrases on disk. Because of
`policy.min_gap_s` (3 s), calls do not fire back to back. The latency of each call matters more than
the lap total.

Results on an 8-core Linux VM, CPU only, default settings:

| | Piper `en_GB-northern_english_male-medium` | Kokoro `bm_george` |
|---|---|---|
| Sample rate | 22050 | 24000 |
| Model load | 1167 ms | 721 ms |
| P1 cold p50 / p99 | 183 / 183 ms | 901 / 901 ms |
| P2 cold p50 / p99 | 144 / 179 ms | 1424 / 1862 ms |
| P3 cold p50 / p99 | 151 / 183 ms | 1386 / 1814 ms |
| Lap cold total, 22 calls | 3.0 s | 29.2 s |
| Budgeted cold total, 20 calls | 2.8 s | 27.4 s |
| Lap audio | 50.6 s | 65.4 s |
| Warm render p99 | 0.0 ms | 0.0 ms |

Kokoro runs on 2 onnxruntime threads (`speech.kokoro_threads`). Piper uses the onnxruntime default
thread count. The game PC has a different CPU, so measure there before you choose an engine.

## Escape hatches, in order of cost

1. Lower the game's UDP rate to 20 Hz and decimate Motion. (free)
2. Below-normal priority and affinity pinning. (free)
3. Move the dashboard off the game PC entirely — phone or tablet. (free)
4. Move the whole backend to a second machine on the LAN — a Pi 5 or a mini-PC handles
   this comfortably, and the design already forbids anything that would prevent it
   (stdlib asyncio, configurable bind address; the Windows-only parts — SAPI speech and the
   keyboard hook — sit behind interfaces with a forwarder/Piper alternative).
5. Rewrite ingest natively (Rust/Go) and keep the strategy engine in Python. The layout
   tables (DC-4) make this a transcription rather than a redesign.

We should expect to need at most the first three. The point of listing all five is that
none of them requires a decision now — the architecture keeps all of them open.

## Operational shape

Ship it as a tray application (PyInstaller single file) that starts with Windows, shows
green/amber/red for "receiving packets", and offers *open dashboard*, *show QR code for
a phone or tablet*, *quiet*, and *stop*. Auto-start on game launch is possible but a manual tray
toggle is less surprising, and it makes "is the backend the reason my frames dropped?" a
one-click experiment.
