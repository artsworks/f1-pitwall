# 0006 — Recording profiles: lite by default, full on demand

Status: accepted (2026-09)

## Context

A full-rate capture of the first live session (Brazil practice, 305 s,
69,233 datagrams) was 69.1 MB, which works out to **~2.45 GB raw per
3-hour session**. Per-packet share of raw bytes at 30 Hz:

| id | packet | rate | raw share |
|---|---|---|---|
| 6 | Car Telemetry | 28.7 Hz | 18.4 % |
| 7 | Car Status | 28.7 Hz | 18.3 % |
| 2 | Lap Data | 28.7 Hz | 17.8 % |
| 0 | Motion | 28.7 Hz | 16.8 % |
| 11 | Session History | 19.1 Hz | 12.4 % |
| 10 | Car Damage | 9.6 Hz | 4.8 % |
| 13 / 16 | Motion Ex / Car Telemetry 2 | 28.7 Hz | 3.5 % each |
| rest | Session, Setups, Participants, Tyre Sets, Lap Positions, Event, … | ≤ 19 Hz | < 3 % |

Observations:

- The engine only reads Session, Lap Data, Event, Car Telemetry, Car Status
  and Car Damage, and evaluates rules at 10 Hz (`engine.tick_hz`). A 30 Hz
  recording is 3× what the rules can use.
- Motion / Motion Ex / Car Telemetry 2 are ~24 % of raw bytes but compress
  poorly: zstd takes the full file 263 MB → 82 MB per 3 h just by dropping
  them. Nothing consumes them yet.
- Compression used to run in a daemon thread after the process exited
  (killed by Ctrl+C) and left the raw file behind, so in practice recordings
  stayed raw.

## Decision

`recording.profile` (settings) / `pitwall start --record <profile>`:

| profile | contents | 3 h raw | 3 h zstd |
|---|---|---|---|
| `full` | every packet, native rate | ~2.5 GB | ~260 MB |
| `lite` (default) | no Motion/MotionEx/Telemetry2; Lap Data, Telemetry, Status, Damage ≤ 10 Hz; Session History 5 Hz, Tyre Sets 2 Hz, Lap Positions 1 Hz | ~0.6 GB | ~35 MB |
| `minimal` | only rule inputs + Participants/Final Classification/Tyre Sets, 5 Hz | ~0.3 GB | ~25 MB |
| `off` | nothing | – | – |

- Filtering is on raw datagrams before the writer, so every profile is an
  ordinary `.f1bin` that replays unchanged; the profile is stamped in the file
  header metadata (`"profile"`). Events are never rate-capped.
- Each finished file is compressed to `.f1bin.zst` and the raw file removed:
  rotated sessions in a background thread, the last one synchronously on exit.
- `pitwall trim FILE --profile lite --out X` downsamples an existing full
  recording.
- `pitwall record` (the dedicated capture tool) defaults to `full`.

Verified: the first live recording trimmed to lite (13,057 datagrams, 0.98 MB
zstd) and minimal (7,002 datagrams, 0.68 MB zstd) both replay to the same
single front-wing-damage call as the full file.

## Consequences

- Use `--record full` when capturing a session to debug a parser/state bug or
  to tune EMAs and thresholds precisely: lite/minimal change the sample rate,
  so EMA values and exact trigger ticks can differ slightly from live.
- Anything that later needs Motion (e.g. a racing-line or lock-up coach) must
  either add those ids to `lite` or be developed from `full` captures.
- Staleness thresholds (`engine.staleness_s`, 0.5 s) must stay above the
  slowest capped rate of a rule input (5 Hz = 0.2 s in `minimal`).
