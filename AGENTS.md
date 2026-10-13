# Agent notes

## Skills

Each skill is a `SKILL.md` file under `.agents/skills/`.

- `engineering-principles`: coding rules for every change in this repo.
- `testing-dashboard`: browser checks of the dashboard and radio pages with real replay recordings.
- `track-overlay-sync`: promote learned values from `~/.pitwall/tracks/*.yaml` or `learning-latest.json` into `src/pitwall/config/defaults/tracks/`.

## Do not commit

These files are personal state or large local data. `.gitignore` covers most of them. Stage files by path and do not use `git add -A`.

- Recordings: `*.f1bin`, `*.f1bin.zst`, `*.f1idx` and `recordings/`.
- Databases: `*.sqlite`, `*.db` and their `-wal` and `-shm` files.
- Learning output: `learnings/`, including `learning-latest.json`.
- Session digests from `~/.pitwall/digests/`.
- Decision logs: `decisions/` and `*.decisions.jsonl`.
- Voice models: `voices/` and `*.onnx`.
