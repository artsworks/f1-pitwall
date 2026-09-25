# Target: friends-only multiplayer league

The goal is to use the assistant in a **friends-only online Grand Prix league**. Single-player
Grand Prix is where it is tested, and it stands in for multiplayer until then. Everything
built for single-player must keep working when some or all rivals are human.

## What changes online

| Area | Single-player | Friends league | Design response |
|---|---|---|---|
| Rival fuel, ERS, brake bias, engine power | visible | **zeroed** for every friend with *Your Telemetry: Restricted* (the default) | per-car `m_yourTelemetry` from Participants gates each rival field; the model treats restricted fields as *unknown*, never as zero |
| Rival tyre wear, wing/floor damage, part wear | visible | zeroed when restricted (Car Damage) | rival stint modelling falls back to **compound + tyre age + lap-time trend**, which stay available |
| Rival tyre sets | visible | player car only | no rival allocation reasoning online |
| Flashback | available | not available online | flashback handling stays in place; it just never fires |
| Pause | player can pause | no pause; network pause via `m_networkPaused` | already handled as a per-car pause |
| Names | AI driver names | "Driver"/placeholder unless each friend enables *Show online ID* (`m_showOnlineNames`) | radio falls back to "car ahead" / team name / race number; optional name aliases in config |
| Mixed grid | all AI | humans + AI fill (`m_aiControlled` per car) | human rivals get a wider pace-variance prior than AI |
| Session | `m_networkGame = 0` | `m_networkGame = 1` | recorded in the session row; the league preset activates automatically |
| Drop-outs | rare | a friend disconnects mid-race | `m_resultStatus` / `m_numActiveCars` purge the car; its slot may be reused, so key rivals by slot **and** network id |

**Assumption: friends stay Restricted**, like a real race where rival data is hidden. The
two friends' cars are modelled from compound, tyre age, stint length and lap-time trend;
the AI cars (the rest of the grid) remain fully visible. Rival fuel and energy calls
("he's low on battery") are therefore only made about AI cars, and the radio says so
when the target is a human with hidden data.

## League preset

A `league` profile layered over the user profile (`08-configuration.md`):

- rival models run in "lap-time only" mode for restricted cars;
- rival names come from an alias map (`gamertag → "Dave"`), since online IDs are
  often off;
- recording redacts online IDs by default;
- a **facts-only** option for leagues that decide strategy coaching is off-limits.
  Priority-1 facts (safety car, damage, fuel, penalties) still speak; recommendations
  don't.

## Fairness

Friends leagues settle this socially, not technically. If the assistant is in use,
say so, and offer the repo. Under GPLv3 your friends can run it too.

## Test and feedback loop (single-player GP)

The pilot is tested by racing single-player Grand Prix and reporting what you notice. To
make that cheap:

1. **Record by default.** Every session is kept, tagged with game mode, track and
   config hash, so any report can be replayed exactly.
2. **Mark a moment in the race.** Long-press button 2 or spacebar (≥ 800 ms) drops a
   bookmark in the recording, with no speech and no effect on the race. Afterwards, go
   through the bookmarks and write what was wrong.
3. **Post-session feedback screen.** The dashboard lists bookmarks and graded calls
   next to the radio log, with a free-text box for each. Saved to SQLite and exported
   to Markdown.
4. **`pitwall report <session>`** bundles the trimmed recording around each bookmark,
   the decision log, and the notes into a zip, ready to attach to a GitHub issue.
5. **Issue templates** in the repo: *wrong call*, *missed call*, *too chatty*, *bug*,
   *idea*. Each asks for the report bundle.

With a bug report plus its recording, the fix gets a replay test, and the same bug can't
come back unnoticed.

## Staging

| Stage | Where | Proves |
|---|---|---|
| M0–M2 | single-player GP, AI grid | ingest, replay, calls, input, feedback loop |
| M3 | single-player GP with **restricted rival simulation**: replay a single-player recording with rival fuel/ERS/wear fields masked | the model degrades gracefully without rival telemetry |
| M3+ | first friends-league race, recorded | real mixed grid, names, drop-outs |
| M4 | tuning from league recordings | human-rival pace priors |

The masking replay (`pitwall replay --mask-restricted`) lets online conditions be tested
without needing friends in a lobby.
