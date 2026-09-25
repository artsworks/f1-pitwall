# Replay, review and debugging

Short answer to "do we have replayability?": yes — it is milestone zero, before anything
speaks. This document specifies it properly, because it is the tool the whole project is
tuned with.

## What gets recorded

Recording is **on by default** and happens before parsing, so a recording is valid even
if the parser is wrong — which matters most in the early weeks, when the parser *will* be
wrong and you will want to re-run old sessions through a fixed one.

`.f1bin` layout:

```
header:  magic "F1BIN\0" | uint16 file_version | uint16 packet_format
         uint64 session_uid | uint64 wall_clock_start_us | uint32 config_hash
         uint16 game_version | uint16 reserved | utf8 json blob (host, settings, notes)
record:  uint32 offset_us (from wall_clock_start) | uint16 length | bytes payload
```

Sidecar `.f1idx`, written on close (and rebuildable): byte offsets for every lap start,
session-type change, pit entry/exit, and every Event packet. This is what makes "jump to
lap 24" and "jump to the safety car" instant on a ~1 GB file rather than a linear scan.

A **new file per session UID**, rotated on `SEND`/Final Classification. A retention
policy in config (default: keep everything under N GB, delete oldest, never delete a file
tagged `keep`). Tagging is one click in the UI during or after a session — "that was the
race where the undercut call was wrong" is a thing you know at the time and forget by the
evening.

Raw recording is not the only artefact. Alongside it:

- **`.jsonl` decision log** — one line per rule evaluation that mattered: rule id, tick,
  lap, lap distance, the input values the predicate read, fired or suppressed, and *which
  suppression layer* suppressed it (cooldown, budget, hysteresis, stale packet, quiet
  mode, revalidation). This log is the single most useful debugging artefact in the
  project, because "why didn't it say anything?" is a more common question than "why did
  it say that?".
- **SQLite rows** for laps, stints, pit events and calls (`03-strategy.md`).

## The replay tool

`pitwall replay <recording> [options]`

| Option | Purpose |
|---|---|
| `--speed 1|10|max` | wall-clock, faster, or as fast as the CPU allows |
| `--from-lap N` / `--to-lap M` | seek via the index |
| `--from-event SCAR` | jump to the first safety car, red flag, pit entry… |
| `--step` | advance one tick at a time, interactively |
| `--pause-on-call` | stop the moment a call fires and print its full input snapshot |
| `--rules config/rules-experiment/` | run a different rule set over the same session |
| `--no-audio` / `--serve` | headless, or serve the dashboard so you can watch it back |
| `--stats` | packet census per type, rates, gaps, malformed counts |
| `--seed-db :memory:` | replay without polluting the real database |

Replay drives the **same ingest entry point** as the live socket. The only difference is
the source object and the clock. Everything time-dependent — EMAs, cooldowns, deadlines,
budgets — reads a `Clock`, which is wall-clock live and virtual in replay, so a 10×
replay produces byte-identical decisions to the 1× one. That determinism is the whole
point; if it ever breaks, the tests stop meaning anything, so there is a CI check that
replays a fixture at 1× and at max and diffs the call streams.

## Review mode (the part that makes it useful for a human)

Replay with `--serve` opens the normal dashboard with a transport bar added:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ◀◀  ▶  ▶▶   LAP 24 / 44   ──●──────────────────────────────  10:23   │
│ calls:  ▌  ▌▌      ▌        ▌▌▌   ▌          ▌▌        ▌             │
│ events: ░░░░SC░░░░           ▌pit                     ▌DRS           │
└──────────────────────────────────────────────────────────────────────┘
```

- Scrub anywhere; the state rebuilds by replaying from the nearest lap boundary.
- Every call is a tick on the timeline. Click it and you get the phrasing, the rule id,
  the predicate that fired, and every snapshot value the predicate read — the "why" is
  reconstructed from the decision log, not guessed.
- Suppressed calls are shown in grey on the same timeline. Seeing what the engine
  *nearly* said is how you tune the budget.
- A "grade this call" control (good / noise / too late / wrong) writes to the database.
  Over a season this becomes a labelled dataset for tuning, and it costs one tap.

## A/B diffing — the tuning loop

```
pitwall diff <recording> --a config/rules --b config/rules-experiment
```

Runs both rule sets over one recording and prints the call-stream diff: calls only A
made, only B made, and those both made with different timing or wording, plus summary
counts per lap and per priority. This turns "does raising the overheat threshold to 108
stop the chatter without missing the real event?" into a five-second question.

Extended to a corpus: `pitwall diff --corpus recordings/*.f1bin` gives the aggregate
effect of a config change over every session ever driven. This is the mechanism by which
the assistant gets better without new races.

## Synthetic and trimmed fixtures

Some situations are hard to produce on demand (red flag, a specific SC timing, a
particular weather crossover). Two answers:

- **Trim tool**: `pitwall trim <recording> --from-lap 22 --to-lap 27 -o fixtures/sc.f1bin`
  — small, committable slices of real sessions. These are the regression fixtures; full
  sessions stay outside git.
- **Synthetic generator**: build valid packets from the layout tables to script an exact
  scenario. Used only for cases real recordings do not cover, and always flagged as
  synthetic, because synthetic data quietly encodes your assumptions.

## Debugging when it is live

- `--stats` equivalent on a live `/debug` page: packets/s per type, malformed count,
  unknown header triples, per-car staleness for the round-robin packets, tick duration
  histogram, latency histograms per stage, queue depth, calls-per-lap versus budget.
- `pitwall doctor`: binds the port, checks the Windows Firewall rule, waits for packets,
  reports the format and rate actually observed, and prints exactly which in-game setting
  is wrong when it sees nothing. This is the difference between a five-minute setup and
  an evening lost to a firewall dialog.
- Every recording stores the `config_hash`, so a replay can warn when the current config
  differs from the one that was live — otherwise you will eventually debug a call that
  the running config could never have produced.
