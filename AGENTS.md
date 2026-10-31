# Agent notes

## Skills

Each skill is a `SKILL.md` file under `.agents/skills/`.

- `engineering-principles`: coding rules for every change in this repo.
- `testing-dashboard`: browser checks of the dashboard and radio pages with real replay recordings.
- `tts-engine-contract`: the speech engine contract, capability wiring, WAV rules and required tests. Read it before you change `src/pitwall/audio/` or speaker wiring.
- `track-overlay-sync`: promote learned values from `~/.pitwall/tracks/*.yaml` or `learning-latest.json` into `src/pitwall/config/defaults/tracks/`.

## Speech engines

A speech engine (TTS) change must keep these working:

- the dispatcher call lifecycle
- dashboard telemetry
- local playback
- browser audio streaming
- replay

Do not add engine-specific checks, such as `isinstance` on an engine class or a test on an engine name, to `src/pitwall/cli/serve.py`, `src/pitwall/cli/recordings.py` or `src/pitwall/server/`. Use the capability flags and `wire_speaker()` from `src/pitwall/audio/speaker.py`.

## Do not commit

These files are personal state or large local data. `.gitignore` covers most of them. Stage files by path and do not use `git add -A`.

- Recordings: `*.f1bin`, `*.f1bin.zst`, `*.f1idx` and `recordings/`.
- Databases: `*.sqlite`, `*.db` and their `-wal` and `-shm` files.
- Learning output: `learnings/`, including `learning-latest.json`.
- Session digests from `~/.pitwall/digests/`.
- Decision logs: `decisions/` and `*.decisions.jsonl`.
- Voice models: `voices/` and `*.onnx`.

## Bench

A full `pitwall bench` run replays every scenario, takes many minutes and uses a large share of the ACU allowance. CI cannot run the bench, because recordings stay out of git.

In a cloud session:

- Run only `pitwall bench --only <id> ...` with the scenarios that your change touches. For example, a fix to the overcut rule runs `--only brazil-overcut`.
- Do not run the bench without `--only`.
- Do not pass `--update-baseline` or `--accept-changes`.

The repo owner runs the full bench and updates the baseline on a machine that has the recordings. Read the output that the repo owner pastes.
