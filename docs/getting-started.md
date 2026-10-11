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
uv run pitwall voice say             # you should hear a radio check
uv run pitwall start                 # ingest + rules + speech + dashboard
```

## Natural voice (Piper)

The built-in Windows voices sound robotic. Download a Piper neural voice once
(~60 MB, into `voices/`, git-ignored); `pitwall start` then uses it automatically
and falls back to SAPI if it cannot load:

```powershell
uv run pitwall voice get                      # default: en_GB-northern_english_male-medium
uv run pitwall voice get en_US-ryan-high      # try others; samples: https://rhasspy.github.io/piper-samples/
uv run pitwall voice warm                     # pre-render 50 common fixed phrases
uv run pitwall voice                          # list installed voices
uv run pitwall voice say --engine piper --voice en_US-ryan-high "Box this lap."
uv run pitwall voice say --engine sapi        # compare with the old voice
```

Pick the default in `speech.piper_voice` and the pace in `speech.piper_speed`
(1.3 default; try `voice say --speed 1.4`). `speech.rate` and `speech.volume`
apply to both engines. Piper also reads each call in a tone set by its priority:
P1 urgent (`speech.tone_urgent_speed` / `tone_urgent_expression`), P2 normal and P3 calm
(`tone_calm_*`). Expression scales Piper's pitch and energy variation (1.0 is the voice's
default).

Open `http://localhost:8000` on the second monitor (`/radio` for the compact log).
You should hear "Pit wall online." at start.

## Phone radio over HTTPS

On the PC that hosts Pitwall, install [mkcert](https://github.com/FiloSottile/mkcert)
and run `mkcert -install`. Make a certificate for the PC's actual LAN IP:

```powershell
mkcert -cert-file "$HOME\.pitwall\phone.pem" -key-file "$HOME\.pitwall\phone-key.pem" 192.168.1.20 localhost 127.0.0.1
```

Replace `192.168.1.20` with the PC's IP. Configure `connection.https_cert` and
`connection.https_key` with those absolute PEM paths and restart Pitwall. Install
mkcert's local root CA on the phone (the mkcert README explains the phone-specific
steps); the phone must trust it for wake lock, service worker, and PWA install.
Open `https://<PC-LAN-IP>:8000/radio`, choose **ARM PHONE RADIO** once on the
phone, then install the page from the browser menu if desired. Use phone speech
when backend speech is off to avoid hearing duplicate calls. Browsers may suspend
speech in background tabs; keep the page open and visible. The PWA caches only
static assets; live telemetry still requires a connection to the PC.

Other devices need the 4-digit PIN that Pitwall prints at startup. The PIN changes on each start. After 5 wrong tries, that device waits 60 s. This PC needs no PIN, unless the request came through a proxy that adds `X-Forwarded-For` or `Forwarded`. If a tunnel or port forwarder on this PC serves the dashboard, set `connection.pin_trust_localhost: false`. Then this PC needs the PIN too. Set `connection.require_pin: false` to turn the PIN off. The PIN cookie is sent only over HTTPS when `connection.https_cert` is set. Do not forward port 8000 to the internet.

## After a session

Nothing is required after a live session. Pitwall grades it when it ends. Then
automatic upkeep runs on the next `pitwall start`. It grades any remaining sessions and
rebuilds stint priors once per learning version. It moves bad learned values to SQLite
quarantine with a reason such as `unknown_track` or `deg_clamped`. It also refits track
priors and adjusts rule cooldowns. `pitwall doctor` reports the quarantine
count.

Open `http://localhost:8000/debrief` to list stored sessions. Each row links to its
debrief, where you can grade calls. `/debrief/latest` opens the newest session.

These commands are optional:

| Command | Purpose |
|---|---|
| `pitwall sessions` | List stored sessions, newest first |
| `pitwall debrief --session latest` | Export a session review as HTML |
| `pitwall stats --learned` | Show learned values and their sources |
| `pitwall calibrate` | Fit track values now (upkeep also runs this fit) |
| `pitwall evaluate` | Compare calls-on and calls-off sessions |
| `pitwall propose` | Write threshold candidates for review |
| `pitwall cleanup` | Delete old recordings, learning packs and caches. Lists them and asks first |
| `pitwall digest` | Write a digest JSON or ingest external recordings |

Use `pitwall digest <paths...>` when importing recordings from elsewhere. You do not
need to run it for a live session or edit SQLite.

Cleanup lists files older than 5 days by default. Use `--days` to change the age.
Cleanup keeps recordings until that exact file has been imported. Another file with
the same session ID is not enough. It also keeps files changed after import and files
written within the last hour. Check the list before confirming deletion.
To also delete old recordings that are not learned yet, add `--include-unlearned`.
Pitwall cannot learn from a recording after you delete it.
Cleanup also removes a `.f1bin` copy when its `.f1bin.zst` sibling has identical data and both
files are at least an hour old.
In `learnings/`, cleanup removes dated `learning-YYYY-MM-DD.json` packs older than `--days`
and leftover `.tmp` files. It never removes `learning-latest.json` or `track_ledger.jsonl`.

## Recording

| Command | Use |
|---|---|
| `uv run pitwall start` | `lite` profile: normal play, ~35 MB per 3 h |
| `uv run pitwall start --record full` | every packet at 30 Hz, for debugging and tuning |
| `uv run pitwall start --record minimal` / `off` | smallest useful capture / none |

Files land in `recordings/` as `.f1bin.zst` + `.f1idx`; they stay on your PC and are
never committed ([ADR 0005](adr/0005-recordings-never-committed.md)). It is safe to delete
them while pitwall is stopped.

List recordings, newest first:

```powershell
uv run pitwall recordings
```

Commands that take a recording (`replay`, `stats`, `trim`, `report`, `digest`, `tune`,
`calibrate` and `diff`) accept any of these:

- nothing or `latest`, for the newest recording
- the `#` from `pitwall recordings`, such as `1` for the one before the newest
- a file name in `recordings/`, with or without `.f1bin` or `.f1bin.zst`
- a path

The command prints the file it picked. Replay offline:

```powershell
uv run pitwall replay --speed 4 --serve          # newest recording
uv run pitwall replay 2 --from-lap 10            # third newest, from lap 10
```

`--from-lap` uses the `.f1idx` file next to the recording. Pitwall rebuilds that file
when it is missing or older than the recording.

## Troubleshooting

- **Doctor shows 0 datagrams**: set the in-game IP to `127.0.0.1`, restart the game after changing telemetry settings, and allow UDP 20777 through Windows Firewall (doctor prints the `netsh` commands).
- **Dashboard says STALE**: no fresh telemetry for over 1 s — you are in a menu, paused, or the game is not sending.
