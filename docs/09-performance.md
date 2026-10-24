# Running on the game PC without costing frames

The backend shares a machine with F1 26. This document answers "how do we make it light".
Define *light* with measurements, not impressions.

## What actually causes stutter

Plan v1 blamed garbage collection. The backend is a separate OS process, so its GC pauses
its own threads, not the game's render loop. The real sources of contention, in order of
impact, are:

1. **CPU core contention.** The game needs its render and simulation threads scheduled
   without delay. A background process that occasionally burns a core can affect 1 % low
   frame times. The effect is worse if the OS schedules it on the render thread's core.
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

Allocating less in the parse loop does not solve these problems. Process-level hygiene
does, at lower cost.

## Budget

| Metric | Target | How measured |
|---|---|---|
| Backend CPU | < 2 % of total on an 8-core machine, steady state | Windows perf counters, logged per session |
| Backend RSS | < 150 MB steady, < 250 MB peak | same |
| Disk write | < 2 MB/s average (expected ~0.25 MB/s at 30 Hz), no single flush > 8 MB | recorder counters |
| Game 1 % low frame time | within noise of a no-backend baseline | PresentMon / CapFrameX, A/B |
| Tick overrun (10 Hz) | p99 < 20 ms of the 100 ms budget | internal histogram |

The fourth row answers the user's question. To measure it, drive the same replayable
benchmark (a hotlap on the same track and settings) three times with the backend off and
three times with it on. Capture frame times and compare 1 % and 0.1 % lows. Run this test
at M1 and M3, then put the numbers in the repo. If the difference is within run-to-run
noise, stop paying for micro-optimisations.

## Measures we take by default

**Process placement**
- Run the backend at **below-normal priority**. The game wins scheduling contests. The
  worst case is a few ms of added call latency, within the budget.
- Optionally pin the backend to the last one or two logical cores (config:
  `cpu_affinity`). Measure before enabling because results depend on the CPU. On CPUs
  with efficiency cores, pinning to those is usually best.
- Never touch the global timer resolution.

**Network**
- When co-hosted, set the game's UDP IP to **`127.0.0.1`** with broadcast off. Loopback
  skips the NIC driver and removes the LAN as a failure mode. The phone still reaches the
  dashboard at the PC's LAN address over TCP. The paths are independent.
- A single socket with a large receive buffer (`SO_RCVBUF` ~4 MB) so a brief stall never
  drops packets; count and expose drops.

**Hot path**
- One thread reads the socket, parses packets, and updates state. Use no locks on it.
- `struct.Struct.unpack_from` into preallocated `__slots__` objects; no per-packet dicts
  or lists; reuse a `bytearray` receive buffer.
- Run `gc.freeze()` after warm-up. Disable automatic GC during green-flag running and
  call `gc.collect()` at safe moments (in the garage, during a pause, between sessions).
  The v1 instinct was right here, but needed a different justification and narrower scope.
- Not all packets need full parsing every frame. Motion at 60 Hz provides context only.
  Parse it at a lower rate or skip it unless a rule needs it. The cheapest optimisation is
  to **lower the in-game send rate to 20–30 Hz**. Only thermal smoothing benefits from
  60 Hz, and the slow EMA does not. Document the rate and recommend 30 Hz by default.

**I/O off the hot path**
- Recorder: append to a buffered writer on a dedicated thread fed by a bounded queue.
  Flush in ~1 MB chunks, never `fsync`. If the queue overflows, drop recording rows and
  show a UI badge. Recording must never back-pressure ingest.
- SQLite: WAL mode, `synchronous=NORMAL`, one background writer thread, batched at lap
  boundaries rather than per row.
- Use a different physical drive from the game install when possible. Add a Defender
  exclusion for the recording directory because real-time AV scanning adds cost.
- Compress finished recordings (zstd) in a below-normal-priority job after the session
  closes, never during running.

**Client**
- The dashboard is on a **second monitor** driven by the game PC, so it shares the game's
  GPU. Keep the page cheap: static text at a 5 Hz state rate, no canvas or charts in race
  mode, no animations, gradients, or shadows, `content-visibility`, and a
  non-fullscreen-composited window. Enable hardware acceleration in that browser. Measure
  its cost in the frame-time A/B because it may cost more than the backend.
- State at 5–10 Hz, not 60 Hz; deltas rather than full snapshots where it is easy.

## Speech latency

`tts_ms` measures the time from dispatcher handoff to the start of local playback. The dispatcher calls `Metrics.note_handoff()` when it hands a call to the speaker. The speaker calls `on_spoken` when playback starts, and `wire_speaker()` passes that time to `Metrics.note_spoken()`. On a phrase-cache miss, `tts_ms` includes the render time. Time in the dispatcher queue is a separate metric, `queue_wait_ms`. The dashboard `latency` block shows both, as `tts_p99_ms` and `queue_wait_p99_ms`.

Engine adapters must keep this measurement:

- Call `on_spoken` when playback starts, not when rendering ends.
- Call `on_spoken` once per call, and never for a dropped call.
- If an engine plays audio itself and cannot report the playback start, it is not a compliant speaker. Put a renderer in front of it that returns WAV bytes for `WavSpeaker`.

## Escape hatches, in order of cost

1. Lower the game's UDP rate to 20 Hz and decimate Motion. (free)
2. Below-normal priority and affinity pinning. (free)
3. Move the dashboard off the game PC to a phone or tablet. This option is free.
4. Move the whole backend to a second machine on the LAN. A Pi 5 or mini-PC handles this
   comfortably. The design already allows this. It uses stdlib asyncio and a configurable
   bind address. The Windows-only parts are SAPI voice-command recognition and the
   keyboard hook. Speech output uses Piper, which also runs on Linux. Put the
   Windows-only parts behind interfaces with a forwarder.
5. Rewrite ingest natively (Rust/Go) and keep the strategy engine in Python. The layout
   tables (DC-4) make this a transcription rather than a redesign.

Expect to need at most the first three. The architecture keeps all five options open, so
none requires a decision now.

## Operational shape

Ship a tray application (PyInstaller single file) that starts with Windows, shows
green/amber/red for "receiving packets", and offers *open dashboard*, *show QR code for
a phone or tablet*, *quiet*, and *stop*. Auto-start on game launch is possible, but a
manual tray toggle is less surprising. It also makes "is the backend the reason my
frames dropped?" a one-click experiment.
