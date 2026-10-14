# Getting started

Run pitwall on the Windows game PC next to F1 26.

## Install

Install Git and [uv](https://docs.astral.sh/uv/). `uv` downloads Python 3.12. Run these
commands in PowerShell:

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

## Speech engines

`speech.engine` in `settings.yaml` picks the engine. On Windows, the default `auto` uses Piper when its voice file exists, else SAPI. On other systems, `auto` uses `null`. Set `piper` or `omnivoice` to stream audio from a Linux PC.

| `speech.engine` | Local speech | Browser audio stream |
|---|---|---|
| `piper` | yes | yes |
| `omnivoice` | yes | yes |
| `sapi` | yes, Windows only | no, the phone uses the browser voice |
| `null` | no | no |

Local speech plays only on Windows. On other systems, Piper and OmniVoice play nothing locally but still stream audio to the browser. If an engine fails to load, Pitwall prints a warning and tries its fallback. An unknown engine id prints a warning, and Pitwall uses `auto`. [Configuration](08-configuration.md) lists the fallbacks and the fields for each engine.

## Natural voice (Piper)

The built-in Windows voices sound robotic. Download a Piper neural voice once
(~60 MB, into `voices/`, git-ignored); `pitwall start` then uses it automatically
and falls back to SAPI if it cannot load:

```powershell
uv run pitwall voice get                      # default: en_GB-northern_english_male-medium
uv run pitwall voice get en_US-ryan-high      # try others; samples: https://rhasspy.github.io/piper-samples/
uv run pitwall voice warm                     # pre-render common fixed phrases (default 150)
uv run pitwall voice                          # list installed voices
uv run pitwall voice say --engine piper --voice en_US-ryan-high "Box this lap."
uv run pitwall voice say --engine sapi        # compare with the old voice
```

`speech.warm_phrases` sets how many phrases `voice warm` pre-renders. The default is 150.
If disk space is tight, lower it in `~/.pitwall/profile.yaml`:

```yaml
speech:
  warm_phrases: 50
```

`voice warm` caches startup lines, fixed rule lines, severity and escalation variants,
driver replies, and menu labels. Lines with slots such as `{lap}` cannot be cached. Run
`voice warm --replay latest` to cache lines from a real session. It skips lines with
driver names or measured values, such as gaps and battery levels, because those rarely
repeat. At startup, Pitwall renders the startup lines with the selected engine before it
speaks them.

Pick the default with `speech.piper_voice`. Set speed with `speech.piper_speed`, default
1.3. Try `voice say --speed 1.4`. `speech.rate` and `speech.volume` apply to both engines.
Piper sets each call's tone by priority. P1 is urgent (`speech.tone_urgent_speed` /
`tone_urgent_expression`), P2 is normal, and P3 is calm (`tone_calm_*`). Expression scales
Piper's pitch and energy variation. The default is 1.0.

## Natural voice (OmniVoice, opt-in)

Run the game and Pitwall on the Windows PC. Run OmniVoice's `tts-server` on a Linux PC with a GPU. Pitwall can also run on Linux. Linux Pitwall plays no local audio and streams speech to a browser or phone.

Set these values under `speech`:

```yaml
engine: omnivoice
omnivoice_url: "http://192.168.1.30:8080"
omnivoice_timeout_s: 2.5
omnivoice_wait_s: 5.0
```

Start `tts-server` with `--locked`, `--ref-audio` and `--ref-text`. The server owns the voice, seed and steps. Pitwall sends only the text.

Phrase WAV files live in `voices/.phrases`. The cache key changes when the text, the server model or `radio_click` changes. After you change the voice, seed or steps on the server, delete `voices/.phrases`.

A cold line waits up to `omnivoice_wait_s` (default 5 seconds) for OmniVoice audio. If OmniVoice is slow or fails, Pitwall plays Piper and renders OmniVoice in the background. Later requests play the cached OmniVoice audio. Set `omnivoice_wait_s: 0` to play Piper at once. `omnivoice_timeout_s` is the timeout for the startup check of the server.

Warm common lines or lines from a recording:

```powershell
uv run pitwall voice warm --engine omnivoice
uv run pitwall voice warm --engine omnivoice --replay latest
```

At startup, Pitwall waits for OmniVoice to render the startup lines before it speaks them.
It plays Piper only if that render fails.

`voice warm --engine omnivoice` then prints the OmniVoice render times: min, p50, p95 and max. Warming uses the background path, so these times are the full render times. Use the p95 to choose `omnivoice_wait_s`. A cold line that takes longer than `omnivoice_wait_s` plays Piper instead.

Add `--log-level` before the command to print more detail. `--log-level info` prints OmniVoice render stats every 10 renders. `--log-level debug` also prints each render time and the UDP packet counts every 10 seconds. For example, run `uv run pitwall --log-level debug start`.

The `voices/` directory is git-ignored. Copy `voices/.phrases` to friends who use the same server model and voice settings. Their Pitwall still needs a reachable OmniVoice server. Without one, Pitwall falls back to Piper.

OmniVoice falls back to Piper, then SAPI on Windows or null on other systems.

Open `http://localhost:8000` on the second monitor (`/radio` for the compact log).
You should hear "Pit wall online." at start.

## Phone radio over HTTPS

On the Pitwall PC, install [mkcert](https://github.com/FiloSottile/mkcert) and run
`mkcert -install`. Make a certificate for the PC's actual LAN IP:

```powershell
mkcert -cert-file "$HOME\.pitwall\phone.pem" -key-file "$HOME\.pitwall\phone-key.pem" 192.168.1.20 localhost 127.0.0.1
```

Replace `192.168.1.20` with the PC's IP. Set `connection.https_cert` and
`connection.https_key` to those absolute PEM paths, then restart Pitwall. Install mkcert's
local root CA on the phone. The mkcert README explains the phone-specific steps. The phone
must trust the CA for wake lock, service worker, and PWA installation.

Open `https://<PC-LAN-IP>:8000/radio`. On the phone, choose **ARM PHONE RADIO** once.
Install the page from the browser menu if desired. Use phone speech when backend speech is
off to avoid duplicate calls. Browsers may suspend speech in background tabs, so keep the
page open and visible. The PWA caches only static assets. Live telemetry still needs a
connection to the PC.

Other devices need the 4-digit PIN that Pitwall prints at startup. The PIN changes on
each start. After 5 wrong tries, a device waits 60 s. This PC needs no PIN unless a proxy
adds `X-Forwarded-For` or `Forwarded`. If a tunnel or port forwarder on this PC serves the
dashboard, set `connection.pin_trust_localhost: false`. This PC then needs the PIN too.
Set `connection.require_pin: false` to disable the PIN. The PIN cookie is sent only over
HTTPS when `connection.https_cert` is set. Do not forward port 8000 to the internet.

## After a session

No action is required after a live session. Pitwall grades it when it ends. Automatic
upkeep runs on the next `pitwall start`. Upkeep grades remaining sessions and rebuilds
stint priors once per learning version. It moves invalid learned values to SQLite
quarantine with reasons such as `unknown_track` or `deg_clamped`. Upkeep also refits track
priors and adjusts rule cooldowns. `pitwall doctor` reports the quarantine count.

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

### Reclaim disk space

1. Run `uv run pitwall cleanup --days 1 --learn`. Cleanup learns recordings older than
   `--days` and at least an hour old. It skips live recordings, then lists what it can delete.
2. If you also want to delete recordings that Pitwall cannot learn, run `uv run pitwall cleanup --days 1 --include-unlearned`.

When cleanup keeps unlearned recordings, it prints `learn them` and `delete anyway` commands.
Run `uv run pitwall digest recordings/` to learn recordings without planning a cleanup.

Recordings named `session_0000000000000000_*` have no session UID. Digest reports
`recording did not contain a session UID` for them and does not learn them. Cleanup
deletes them only with `--include-unlearned`. You can also delete them by hand.
Cleanup never deletes split archives (`.zip.*` files) or decision logs such as
`balanced.decisions.jsonl`. Delete those by hand.

## Recording

| Command | Use |
|---|---|
| `uv run pitwall start` | `lite` profile: normal play, ~35 MB per 3 h |
| `uv run pitwall start --record full` | every packet at 30 Hz, for debugging and tuning |
| `uv run pitwall start --record minimal` / `off` | smallest useful capture / none |

Files land in `recordings/` as `.f1bin.zst` + `.f1idx`; they stay on your PC and are
never committed ([ADR 0005](adr/0005-recordings-never-committed.md)). It is safe to delete
them while pitwall is stopped.

Personal state (SQLite database, digests, profile, track overlays and certificates) lives in
`~/.pitwall`, outside git and shared by every checkout, while `recordings/`, `learnings/` and
`voices/` sit in the working directory and git ignores them.

List recordings, newest first:

```powershell
uv run pitwall recordings
```

Commands that take a recording (`replay`, `stats`, `report`, `digest`, `tune`,
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
- If the dashboard says STALE, Pitwall has received no fresh telemetry for over 1 s.
  You may be in a menu, paused, or the game may not be sending.
