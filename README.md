# f1-pitwall

A race engineer for F1 26. It reads the game's UDP telemetry, keeps a model of the
session, decides what is worth saying, says it over your headset, and shows it on a second monitor.

Read the plan online: https://artsworks.github.io/f1-pitwall/

## Quick start (game PC)

```bash
uv sync
uv run pitwall doctor
uv run pitwall start
```

In the game, set **Settings → Telemetry**:

| Setting | Value |
|---|---|
| UDP Telemetry | On |
| UDP IP | `127.0.0.1` (or the pitwall PC's IP) |
| UDP Port | `20777` |
| UDP Send Rate | `30 Hz` |
| UDP Format | `2026` |

`pitwall doctor` checks the port, the wire format, the observed rate, and speech.
Then `pitwall start` runs ingest + rules + speech and serves the dashboard —
open `http://localhost:8000` on the second monitor (`/radio` is a log-only
large-type view). M1 acceptance: on an out-lap with a cold front-left you hear
the call within 300 ms.

## Recording and replay

Recording is automatic: every session lands under `recordings/` as
`.f1bin` (+ `.f1idx` index and a `.decisions.jsonl` decision log). Replay it
through the same engine offline:

```bash
uv run pitwall replay recordings/<file>.f1bin --speed 10 --stats
uv run pitwall replay recordings/<file>.f1bin --serve   # watch it on the dashboard
```

## Why it is not just another dashboard

Gauges are a solved problem. The value here is judgement: knowing that the undercut is
worth 1.2 seconds, that there are three laps of life left in the fronts at this pace, and
— above all — knowing when to stay silent. The design is built around two things that
make that achievable:

- **Record and replay.** Every session is captured raw. Strategy logic is tuned and tested
  offline against recordings instead of by driving another race.
- **Rules as data.** Every call is a declarative rule with its own hysteresis, cooldown
  and phrasing, so tuning is a config edit and every rule has a replay test.
- **A mindset you set.** Balanced or aggressive for the pilot — one control that biases
  judgement across pit windows, energy, tyre management and which rival it watches.

## Documents

| Document | Contents |
|---|---|
| [`docs/00-review-of-v1.md`](docs/00-review-of-v1.md) | Review of the first plan: spec errors, gaps, architectural changes |
| [`docs/01-architecture.md`](docs/01-architecture.md) | Layers, stack, cross-cutting decisions, repo layout |
| [`docs/02-ingestion.md`](docs/02-ingestion.md) | Game settings, packets consumed, parser design, pause/flashback, recording format |
| [`docs/03-strategy.md`](docs/03-strategy.md) | Rule format, pace and degradation model, pit loss, race and quali calls, persistence |
| [`docs/04-audio-ui.md`](docs/04-audio-ui.md) | Priorities, suppression, speech constraints, WebSocket protocol, dashboard |
| [`docs/05-roadmap.md`](docs/05-roadmap.md) | Milestones M0–M4 as vertical slices |
| [`docs/06-direction-changes.md`](docs/06-direction-changes.md) | The five structural changes from v1, each with cost, build plan, acceptance and rollback |
| [`docs/07-replay-and-debug.md`](docs/07-replay-and-debug.md) | Recording format, replay CLI, review mode, A/B diffing, live diagnostics |
| [`docs/08-configuration.md`](docs/08-configuration.md) | Settings model, verbosity presets, and the balanced/aggressive mindset vector |
| [`docs/09-performance.md`](docs/09-performance.md) | Co-hosting with the game: what actually causes stutter, the budget, and how it is measured |
| [`docs/10-angles-not-yet-considered.md`](docs/10-angles-not-yet-considered.md) | Twenty angles neither plan covered, with recommendations |
| [`docs/11-open-questions.md`](docs/11-open-questions.md) | Decisions still to make |
| [`docs/12-driver-input.md`](docs/12-driver-input.md) | Wheel button and spacebar: acknowledge / negative, and adaptivity without an LLM |
| [`docs/13-league-multiplayer.md`](docs/13-league-multiplayer.md) | Friends-league target: restricted telemetry, league preset, test-and-feedback loop |
| [`docs/14-adversarial-review.md`](docs/14-adversarial-review.md) | Adversarial review before the first commit: errors found, what was kept, assumptions to verify |
| [`docs/reference/f1-26-udp-notes.md`](docs/reference/f1-26-udp-notes.md) | Verified format-2026 packet facts the design relies on |
| [`docs/original-plan-v1.md`](docs/original-plan-v1.md) | The original plan, kept for provenance |

## Shape of the system

```
F1 26 (Windows PC) ──UDP 20777──▶ ingest ──▶ session state ──▶ rules ──▶ dispatcher
                                     │             │             │           │
                                 recorder      SQLite        decision   WebSocket 8000
                                  (.f1bin)                      log          │
                                     │                                  2nd monitor (UI)
                                  replay ◀── tests / offline tuning     speech: backend
```

## Next step

Answer the open questions, then M0: capture and replay.

## Licence

GPLv3 — see [`LICENSE`](LICENSE).
