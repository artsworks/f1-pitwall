# Security model

This file describes who can reach pitwall, what matters, and which risks are in scope. Security scans and reviews start from it.

## Components

| Name | Code | Trust boundary |
| --- | --- | --- |
| pitwall-server | `src/pitwall/server/` (`app.py`, `pin.py`, `review.py`, `hub.py`), `web/` | HTTP and WebSocket on the LAN |
| pitwall-ingest | `src/pitwall/net/`, `src/pitwall/protocol/`, `src/pitwall/state/`, `src/pitwall/ingest.py`, `src/pitwall/ops/supervisor.py` | UDP telemetry on the LAN |
| pitwall-core | engine, rules, strategy, model, learning, store, config, audio, voice, input and setup under `src/pitwall/` | local process that reads local files and SQLite |
| pitwall-cli | `src/pitwall/cli/` (`cert.py`, `update.py`, `serve.py`), `install/`, `scripts/` | local operator, runs subprocesses |
| docs-site | `docs/` | public static site on GitHub Pages |

## System overview

Pitwall is a Python app that runs on the driver's Windows game PC.

- The UDP telemetry listener binds `connection.udp_host` = `0.0.0.0`, port 20777. The F1 game sends the packets, but any LAN host can send them too.
- The FastAPI dashboard binds `connection.http_host` = `0.0.0.0`, port 8000. Phones and tablets on the LAN open it.
- `pitwall cert` (mkcert) and `https_cert`/`https_key` turn on HTTPS.
- Pitwall calls a local OmniVoice TTS server at `http://127.0.0.1:8080`.
- `pitwall update` runs `git pull` and `uv sync` (`cli/update.py`).

## Entry points

- pitwall-server serves `GET /`, `/radio`, `/debrief*`, `/api/health`, `/api/config`, `/api/voice/blip.wav`, `/api/review/*` and `POST /api/debrief/{uid}/grade`.
- WebSocket `/ws` takes the messages `hello`, `press`, `audio`, `mindset`, `page`, `menu` and `voice`.
- Any device on the LAN can reach the HTTP and WebSocket entry points.
- The PIN gate (`server/pin.py`) leaves `/pin`, `/api/pin`, `/sw.js` and `/static/*` open.
- pitwall-ingest parses UDP datagrams from any LAN host.
- Recording files (`.f1bin`, `.f1bin.zst`, `.f1idx`) can come from other people.

## Assets

1. Game PC integrity. The network must not drive code execution or file writes through pitwall. This is the most important asset.
2. Local pitwall data: the SQLite database, recordings, decision logs and `~/.pitwall` config. It holds personal race data only.
3. Integrity of the docs site.

## Attackers

The dashboard is reachable on the home LAN only. There is no port forward, tunnel or VPN. Other LAN devices are untrusted.

- A1, another device on the home LAN, such as a guest phone or a compromised IoT device. It can send UDP to port 20777 and HTTP to port 8000 without the PIN cookie.
- A2, a malicious web page in a browser on the game PC or on a LAN device. It can try CSRF or DNS rebinding against `localhost:8000`, where loopback trust applies.
- A3, a person who shares a crafted recording file.
- A4, the supply chain: compromised PyPI or npm packages.
- Out of scope: anyone with a shell on the game PC already has everything pitwall has.

## Existing controls

- The PIN is a random 4-digit number. Five wrong tries lock a host out for 60 s.
- A correct PIN sets an httponly, SameSite=Strict cookie that holds a 32-byte token. Pitwall compares the PIN and the token in constant time.
- Loopback clients skip the PIN when `pin_trust_localhost` is true, which is the default. A request with `X-Forwarded-*` headers loses this trust (`forwarded()` in `pin.py`).
- WebSocket `/ws` checks the PIN before it accepts messages and requires a versioned `hello`.
- The config loader reads YAML with `SafeLoader` only (`config/loader.py`).
- `cli/cert.py` and `cli/update.py` pass subprocess arguments as lists, not with `shell=True`.
- Pitwall only listens to UDP telemetry and only writes files under `~/.pitwall`. It does not spawn or inspect game processes, because of the game's anti-cheat.

## Accepted risk

None. Report every finding, including loopback trust, third-party scripts and dev tooling.
