# Replay, review and debugging

Replayability is milestone zero, before anything speaks. This document specifies the
tool used to tune the whole project.

## What gets recorded

Recording is **on by default** and starts before parsing. A recording remains valid when
the parser is wrong. This matters early, when you may need to replay old sessions through
a corrected parser.

`.f1bin` layout:

```
header:  magic "F1BIN\0" | uint16 file_version | uint16 packet_format
         uint64 session_uid | uint64 wall_clock_start_us | uint32 config_hash
         uint16 game_version | uint16 reserved | utf8 json blob (host, settings, notes)
record:  uint32 delta_us (since previous record) | uint16 length | bytes payload
```

Write the rebuildable `.f1idx` sidecar on close. It stores byte offsets for every lap
start, session-type change, pit entry/exit, and Event packet. This makes "jump to lap 24"
and "jump to the safety car" instant on a ~1 GB file instead of scanning it linearly.

**Profiles** (ADR 0006): `lite` by default (no motion packets, rule inputs capped at
10 Hz, ~35 MB zstd per 3 h), `full` for debugging (`pitwall start --record full`),
`minimal`, or `off`. Compress finished files with zstd and remove the raw file.
If another process locks the raw file, `pitwall cleanup` removes it after an hour once
the compressed copy matches.

Create a **new file per session UID** and rotate it on `SEND`/Final Classification.
Configure retention (default: keep everything under N GB, delete oldest, never delete a
file tagged `keep`). Tag a file with one click in the UI during or after a session.
"That was the race where the undercut call was wrong" is easy to forget by evening.

Raw recording is not the only artefact. Alongside it:

- **`.jsonl` decision log.** Record each relevant rule evaluation with its rule id, tick,
  lap, lap distance, predicate inputs, and fired or suppressed status. Record *which
  suppression layer* applied (cooldown, budget, hysteresis, stale packet, quiet mode, or
  revalidation). "Why didn't it say anything?" is a more common question than "why did it
  say that?".
- **SQLite rows** for laps, stints, pit events and calls (`03-strategy.md`).

## The replay tool

`pitwall replay <recording> [options]`

| Option | Purpose |
|---|---|
| `--speed 1|10|max` | wall-clock, faster, or as fast as the CPU allows |
| `--from-lap N` | seek via the index |
| `--from-us N` / `--to-us N` | limit replay by session time |
| `--mask-restricted` | zero rivals' restricted fuel, ERS and tyre-wear data |
| `--serve` | serve the dashboard so you can watch the replay |
| `--stats` | packet census per type, rates, gaps, malformed counts |
| `--seed-db PATH` | choose replay persistence; defaults to `:memory:` |

Replay uses the **same ingest entry point** as the live socket. Only the source object and
clock differ. Time-dependent systems (EMAs, cooldowns, deadlines, and budgets) read a
`Clock`: wall-clock time live and virtual time in replay. Thus, a 10× replay produces the
same decisions as max speed. If determinism breaks, the tests lose meaning. On every pull
request, CI runs `test_deterministic_across_speeds` and
`test_replay_deterministic_across_speeds`. They replay a synthetic recording at 10× and
max speed, then diff the decision logs.

## Review mode (the part that makes it useful for a human)

Replay with `--serve` opens the normal dashboard with a transport bar added:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ◀◀  ▶  ▶▶   LAP 24 / 44   ──●──────────────────────────────  10:23   │
│ calls:  ▌  ▌▌      ▌        ▌▌▌   ▌          ▌▌        ▌             │
│ events: ░░░░SC░░░░           ▌pit                     ▌OT            │
└──────────────────────────────────────────────────────────────────────┘
```

- Scrub anywhere. The state rebuilds by replaying from the nearest lap boundary.
- Every call is a timeline tick. Click it to see the phrasing, rule id, firing predicate,
  and every snapshot value the predicate read. The decision log reconstructs the "why".
- Show suppressed calls in grey on the same timeline. Review what the engine *nearly*
  said to tune the budget.
- A "grade this call" control (good / noise / too late / wrong) writes to the database.
  The ratings form a labelled tuning dataset over a season. Grading takes one tap.

## Audio in replay

`pitwall replay --serve` builds the speaker from `speech.engine`, the same as `pitwall start`. Each seek or reset rebuilds the engine. Before each rebuild, replay calls `flush()` on a speaker that has it. `flush()` drops queued audio and stops playback. A call from before the flush sends no `audio`, `spoken` or drop update after it, so a reused call id cannot change a later call. Replay then runs `wire_speaker()` for the new engine.

The browser gets `audio` frames only when the engine streams audio. Piper and OmniVoice stream. SAPI and `null` do not.

## A/B diffing: the tuning loop

```
pitwall diff <recording> --a config/rules --b config/rules-experiment
```

Run both rule sets on one recording and print the call-stream diff. It shows calls only A
made, calls only B made, and calls both made with different timing or wording. It also
shows summary counts per lap and priority. This turns "does raising the overheat threshold
to 108 stop the chatter without missing the real event?" into a five-second question.

Run `pitwall diff --corpus recordings/*.f1bin` to see a config change's aggregate effect
across recorded sessions. This lets the assistant improve without new races.

## Synthetic fixtures

Some situations are hard to produce on demand (red flag, a specific SC timing, a
particular weather crossover). Three approaches:

- **Window replay**: `pitwall replay <recording> --from-us N --to-us N` replays one slice of a recording without writing a file.
- **Derived scenarios**: a scenario's `mutations` (`inject_sc`, `vsc`, `wear_scale`, `penalty`) make `pitwall bench` write a synthetic copy of a real recording with a tagged session UID. Use synthetic sessions for grading and tuning, not physics priors. See [Scenario bench](24-scenario-bench.md) and [Synthetic recordings](23-synthetic-recordings.md).
- **Synthetic generator**: build valid packets from the layout tables to script an exact
  scenario. Used only for cases real recordings do not cover, and always flagged as
  synthetic, because synthetic data quietly encodes your assumptions.

## Debugging when it is live

- Show the `--stats` equivalent on a live `/debug` page: packets/s per type, malformed count,
  unknown header triples, per-car staleness for the round-robin packets, tick duration
  histogram, latency histograms per stage, queue depth, calls-per-lap versus budget.
- `pitwall doctor`: bind the port, check the Windows Firewall rule, and wait for packets.
  Report the observed format and rate. If no packets arrive, name the incorrect in-game
  setting. This avoids an evening lost to a firewall dialog.
- Store `config_hash` in every recording. Replay can then warn when the current config
  differs from the live config. Otherwise, you may debug a call that the running config
  could never have produced.
