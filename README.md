# f1-pitwall

A race engineer for F1 26. It reads the game's UDP telemetry, keeps a model of
the session, decides what is worth saying, says it over your headset, and shows
it on a second monitor. Everything runs locally on the game PC; nothing leaves it.

- **Status and milestones:** [docs/status.md](docs/status.md)
- **Docs site:** https://artsworks.github.io/f1-pitwall/
- **Lessons from live sessions (ADRs):** [docs/adr/](docs/adr/README.md)

## Quick start (Windows game PC)

You need Git and [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for
you, so nothing else is required. In PowerShell:

```powershell
# 1. Install uv and Git (skip either if you already have it), then close and reopen PowerShell
winget install --id astral-sh.uv -e
winget install --id Git.Git -e

# 2. Get pitwall
git clone https://github.com/artsworks/f1-pitwall.git
cd f1-pitwall
uv sync                      # creates .venv with Python 3.12 and all dependencies

# 3. Optional: natural voice (~60 MB download into voices/, git-ignored)
uv run pitwall voices get

# 4. Check the setup, then run
uv run pitwall speak         # radio check through your headset
uv run pitwall doctor        # ports, wire format, packet rate, speech
uv run pitwall start         # engine + speech + dashboard
```

Then in F1 26, **Settings → Telemetry Settings**:

| Setting | Value |
|---|---|
| UDP Telemetry | On |
| UDP Broadcast Mode | Off |
| UDP IP Address | `127.0.0.1` |
| UDP Port | `20777` |
| UDP Send Rate | `30 Hz` |
| UDP Format | `2026` |

Open `http://localhost:8000` on the second monitor (`/radio` is the compact
log view). You should hear "Pit wall online." on start, and the dashboard goes
LIVE as soon as you are on track (the game sends nothing from the menus).

### Wheel buttons

In the game's controls menu, bind a free wheel button to **UDP Action 1** (and
optionally another to **UDP Action 3**). The pit wall answers every press by voice.

| Press | Action |
|---|---|
| UDP 1 single | Acknowledge the last call ("Copy.") |
| UDP 1 double | Negative ("Noted."); with no recent call, quiet for 5 minutes |
| UDP 1 held ≥ 0.8 s | **Radio silent** on/off: no speech (urgent P1 calls still speak), dashboard keeps the radio |
| UDP 3 single | Radio silent on/off (use this if the hold doesn't register on your wheel) |

To update later: `git pull; uv sync`.

### If something is off

| Symptom | Fix |
|---|---|
| `winget` not recognised | Use Astral's installer instead: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` (ByPass applies to that one command only) |
| `uv` not recognised after install | Close and reopen PowerShell so the new PATH is picked up |
| Dashboard says STALE | Run `uv run pitwall doctor --seconds 30` *while driving*. `0 datagrams` → check the game settings above, restart the game after changing them, then allow UDP 20777 in Windows Firewall (doctor prints the `netsh` commands). |
| No speech | `uv run pitwall speak` prints the error. `--engine sapi` uses the built-in Windows voice; `--engine piper` the downloaded one. |
| Robotic voice | `uv run pitwall voices get`, then restart `pitwall start`. |

More in [docs/getting-started.md](docs/getting-started.md).

## What it says today

| Call | When |
|---|---|
| Qualifying | Clean-air release, abort advice, and a push/cool/box/push-now run plan with fuel and battery reminders |
| Race strategy | Plans A/B/C, pit windows, undercut and overcut advice, and safety-car or VSC stop calls |
| Tyres | Out-lap checks, thermal advice, and life from the minimum of worst-corner wear life and the pace cliff |
| Fuel and ERS | Fuel margin, deployment and energy-budget calls |
| Driving and flags | Wing damage, boost left on, yellow flags, lock-ups, spins and slides |

Calls rotate through several phrasings, and repeat the same mistake often
enough and the engineer gets drier about it (ADR 0008).

Every call is a rule in [`src/pitwall/config/defaults/rules/shared.yaml`](src/pitwall/config/defaults/rules/shared.yaml)
with thresholds in [`thresholds.yaml`](src/pitwall/config/defaults/thresholds.yaml).

### After a session

No steps are required. Pitwall grades sessions at the end and runs upkeep at the next
start. Optional review and learning commands are in
[After a session](docs/getting-started.md#after-a-session).

## Shape of the system

```mermaid
flowchart LR
    game[F1 26<br/>UDP 20777] --> ingest
    ingest --> rec[(recordings/<br/>.f1bin.zst)]
    ingest --> state[session state<br/>10 Hz snapshots]
    state --> rules[rules<br/>YAML, hysteresis]
    rules --> disp[dispatcher<br/>priority, cooldown, budget]
    disp --> log[(decision log<br/>.jsonl)]
    disp --> speech[speech<br/>Piper / SAPI]
    disp --> ws[WebSocket 8000] --> ui[dashboard<br/>2nd monitor]
    rec -.replay, same path.-> ingest
```

Recordings replay through the same ingest → state → rules path, so every rule
is tuned and tested offline against real sessions instead of by driving another race.

## Recording and replay

Every session is recorded under `recordings/` (local only, git-ignored).
The default `lite` profile is about 35 MB per 3 hours; use `--record full`
when you want a session for debugging.

```powershell
uv run pitwall start --record full                      # everything at 30 Hz
uv run pitwall replay recordings\<file>.f1bin.zst --speed 10 --stats
uv run pitwall replay recordings\<file>.f1bin.zst --serve   # watch it on the dashboard
```

## Why it is not just another dashboard

Gauges are a solved problem. The value here is judgement: knowing that the
undercut is worth 1.2 seconds, that there are three laps of life left in the
fronts at this pace, and, above all, knowing when to stay silent.

- **Record and replay.** Strategy logic is tuned offline against recordings.
- **Rules as data.** Each call is a declarative rule with its own hysteresis,
  cooldown and phrasing, so tuning is a config edit and every rule has a replay test.
- **A mindset you set.** Balanced or aggressive: one control that biases pit
  windows, energy, tyre management and which rival it watches.

## Documents

**Use**

| Document | Contents |
|---|---|
| [`docs/status.md`](docs/status.md) | Milestone progress, what works today, what is next |
| [`docs/getting-started.md`](docs/getting-started.md) | Install, game settings, after-session commands, recording profiles, replay |
| [`docs/07-replay-and-debug.md`](docs/07-replay-and-debug.md) | Recording format, replay CLI, A/B diffing, live diagnostics |
| [`docs/08-configuration.md`](docs/08-configuration.md) | Settings model, verbosity presets, balanced/aggressive mindset |
| [`docs/20-learning-loop.md`](docs/20-learning-loop.md) | Automatic grading and upkeep, calibration, learned state |

**Design**

| Document | Contents |
|---|---|
| [`docs/01-architecture.md`](docs/01-architecture.md) | Layers, stack, cross-cutting decisions, repo layout |
| [`docs/02-ingestion.md`](docs/02-ingestion.md) | Packets consumed, parser design, pause/flashback, recording format |
| [`docs/03-strategy.md`](docs/03-strategy.md) | Rule format, pace and degradation model, pit loss, race and quali calls |
| [`docs/04-audio-ui.md`](docs/04-audio-ui.md) | Priorities, suppression, speech constraints, WebSocket protocol |
| [`docs/09-performance.md`](docs/09-performance.md) | Co-hosting with the game: stutter causes, budget, measurement |
| [`docs/12-driver-input.md`](docs/12-driver-input.md) | Wheel button and spacebar: acknowledge / negative |
| [`docs/13-league-multiplayer.md`](docs/13-league-multiplayer.md) | Friends-league target: restricted telemetry, league preset |
| [`docs/15-dashboard-design.md`](docs/15-dashboard-design.md) | Second-monitor dashboard design (implemented in `web/`) |
| [`docs/16-debrief-design.md`](docs/16-debrief-design.md) | Session debrief: built sections, grading and deferred work |
| [`docs/17-quali-run-plan.md`](docs/17-quali-run-plan.md) | Qualifying run plan and coaching |
| [`docs/18-race-engine.md`](docs/18-race-engine.md) | Race engine, tyre life, degradation priors and persistence |
| [`docs/21-voice-command.md`](docs/21-voice-command.md) | Voice-command design and implementation status |
| [`docs/22-setup-advisor.md`](docs/22-setup-advisor.md) | Setup advisor proposal: parameter effects, parc fermé matrix, balance signals, rule tables, learning loop |

**Decisions and reference**

| Document | Contents |
|---|---|
| [`docs/adr/`](docs/adr/README.md) | Architecture decision records: what live sessions taught us |
| [`docs/05-roadmap.md`](docs/05-roadmap.md) | Milestones M0–M4 as vertical slices |
| [`docs/reference/f1-26-udp-notes.md`](docs/reference/f1-26-udp-notes.md) | Verified format-2026 packet facts |
| [`docs/00-review-of-v1.md`](docs/00-review-of-v1.md), [`06`](docs/06-direction-changes.md), [`10`](docs/10-angles-not-yet-considered.md), [`11`](docs/11-open-questions.md), [`14`](docs/14-adversarial-review.md) | Planning history: review of the first plan, direction changes, open angles and questions, adversarial review |

## Licence

GPLv3 — see [`LICENSE`](LICENSE).
