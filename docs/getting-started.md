# Getting started

Run pitwall on the Windows game PC next to F1 26.

## Install

Git and [uv](https://docs.astral.sh/uv/); uv downloads Python 3.12 itself. In PowerShell:

```powershell
winget install --id astral-sh.uv -e   # then reopen PowerShell
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
apply to both engines. Piper also reads each call in a tone set by its priority:
P1 urgent (`speech.tone_urgent_speed` / `tone_urgent_expression`), P2 normal and P3 calm
(`tone_calm_*`). Expression scales Piper's pitch and energy variation (1.0 is the voice's
default).

## Lighter, more natural voice (Kokoro)

Kokoro-82M sounds more natural than Piper and stays light: it synthesizes on
2 CPU threads (`speech.kokoro_threads`) with no busy-waiting, so the game keeps
its cores and the GPU is never used. Download the model once (~350 MB, into
`voices/`); with it present `pitwall start` prefers Kokoro, then Piper, then SAPI:

```powershell
uv run pitwall voices kokoro
uv run pitwall speak --engine kokoro "Box box, box box. Plan A, hards."
```

The voice is `speech.kokoro_voice` (default `bm_george`; also `bm_lewis`, `bm_daniel`,
`bf_emma`, `am_michael`), pace `speech.kokoro_speed`. P1 and P3 calls use the same
`tone_urgent_speed` / `tone_calm_speed` multipliers; Kokoro has no expression control.

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
