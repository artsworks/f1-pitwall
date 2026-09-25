# Getting started

Run pitwall on the Windows game PC next to F1 26.

## Install

Python 3.12 and [uv](https://docs.astral.sh/uv/):

```powershell
git clone https://github.com/artsworks/f1-pitwall.git
cd f1-pitwall
uv sync
```

## Game settings

**Settings → Telemetry Settings**:

| Setting | Value |
|---|---|
| UDP Telemetry | On |
| UDP Broadcast Mode | Off |
| UDP IP Address | `127.0.0.1` (or this PC's LAN IP) |
| UDP Port | `20777` |
| UDP Send Rate | `30 Hz` |
| UDP Format | `2026` |

## Check, then run

```powershell
uv run pitwall doctor --seconds 30   # while on track: expect "N datagrams, N accepted"
uv run pitwall speak                 # you should hear a radio check
uv run pitwall start                 # ingest + rules + speech + dashboard
```

## Natural voice (Piper)

The built-in Windows voices sound robotic. Download a Piper neural voice once
(~60 MB, into `voices/`, git-ignored); `pitwall start` then uses it automatically
and falls back to SAPI if it cannot load:

```powershell
uv run pitwall voices get                     # default: en_GB-northern_english_male-medium
uv run pitwall voices get en_US-ryan-high     # try others; samples: https://rhasspy.github.io/piper-samples/
uv run pitwall speak --engine piper --voice en_US-ryan-high "Box this lap."
uv run pitwall speak --engine sapi            # compare with the old voice
```

Pick the default in `speech.piper_voice` and the pace in `speech.piper_speed`
(1.3 default; try `speak --speed 1.4`). `speech.rate` and `speech.volume`
apply to both engines.

Open `http://localhost:8000` on the second monitor (`/radio` for the compact log).
You should hear "Pit wall online." at start.

## Recording

| Command | Use |
|---|---|
| `uv run pitwall start` | `lite` profile: normal play, ~35 MB per 3 h |
| `uv run pitwall start --record full` | every packet at 30 Hz, for debugging and tuning |
| `uv run pitwall start --record minimal` / `off` | smallest useful capture / none |

Files land in `recordings/` as `.f1bin.zst` + `.f1idx`; they stay on your PC and are
never committed ([ADR 0005](adr/0005-recordings-never-committed.md)). It is safe to delete
them while pitwall is stopped.

Replay offline:

```powershell
uv run pitwall replay recordings\<file>.f1bin.zst --speed 4 --serve
```

## Troubleshooting

- **Doctor shows 0 datagrams**: set the in-game IP to `127.0.0.1`, restart the game after changing telemetry settings, and allow UDP 20777 through Windows Firewall (doctor prints the `netsh` commands).
- **Dashboard says STALE**: no fresh telemetry for over 1 s — you are in a menu, paused, or the game is not sending.
