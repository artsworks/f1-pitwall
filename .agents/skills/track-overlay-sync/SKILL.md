---
name: track-overlay-sync
description: Promote learned per-track values into the packaged overlays under src/pitwall/config/defaults/tracks/ when the repo owner supplies ~/.pitwall/tracks/*.yaml files or a learning-latest.json pack.
---

# Sync packaged track overlays from learned files

Use this procedure when the repo owner uploads `~/.pitwall/tracks/<id>.yaml` files, `learning-latest.json`, or both.
The goal is one PR that updates `src/pitwall/config/defaults/tracks/<id>.yaml`.

## Sources

- `~/.pitwall/tracks/<id>.yaml` is the output of `pitwall calibrate --write-overlay` or the startup upkeep (`maintenance.maintain`). Both go through `calibrate.write_overlays`.
  These files contain only the learned keys, without `track_id` or `name`.
- The `track_overlays` block in `learning-latest.json` is a copy of those files, keyed by track id (`learnpack._overlays`).
- If both are supplied, the uploaded YAML files are authoritative.
  Check that each file matches its `track_overlays` entry and report any difference.
- Do not re-derive values from `model_params` or other raw pack data.
  Calibrate already applied the promotion gates.

## Rules

`scripts/sync_track_overlays.py` carries out these rules. Run it instead of editing the YAML by hand.

- Skip the `"-1"` entry. It is the bucket for unknown tracks.
- Skip a track that has no overlay entry. Leave its packaged file unchanged.
- Sync only `fuel_kg_per_lap` and the `thresholds` block.
  Merge threshold keys into the packaged `thresholds`. Keep packaged keys that the learned file does not set.
- Keep the hand-maintained fields: `track_id`, `name`, `pit_loss_s`, `pit_exit_m`, `pit_entry_m`, `deg_ms_per_lap` and `base_pace_ms`.
- If a learned file contains `deg_ms_per_lap`, do not copy it. Report it to the repo owner and ask.
  Never promote a `0.0` deg value, because calibrate writes `0.0` for a degenerate fit.
- Do not commit `*.sqlite`, digests or `learnings/`. These files are personal state.
  Stage the changed track files by path. Do not use `git add -A`.

## New track files

- Set `track_id` to the filename stem. `tests/test_config.py::test_packaged_track_overlays_validate` checks this.
- Take `name` from the lowercase venue names that the other packaged files use, such as `melbourne`, `spa` and `spielberg`.
  The id-to-track map is in `protocol/enums.py` (`TrackId`) and `debrief.py` (`_TRACK_NAMES`).
  If the venue name is not obvious, ask the repo owner.
- Add only `track_id`, `name` and the learned keys. `TrackOverlay` in `config/models.py` gives defaults for the rest.

## Steps

1. Do not edit `src/pitwall/config/defaults/tracks/*.yaml` by hand.
   Run the script so the format and merge rules stay consistent.
2. Save the uploaded pack to `learnings/learning-latest.json`, or pass its path with `--pack`.
   Put uploaded YAML files in a directory and pass it with `--overlay-dir`.
3. Run `uv run python scripts/sync_track_overlays.py --dry-run`. Read the change table and every `WARN:` line.
4. Stop and ask the repo owner when any of these happen:
   - the script warns about `deg_ms_per_lap`
   - the script warns about a home/pack mismatch
   - a track id has no name
   - a temperature moved by more than 5 C, or energy by more than 100000 J
5. Run the script without `--dry-run` on a clean tree.
   Pass `--yes` only in a fresh session or after the owner approves.
6. In a cloud session the agent opens PRs through its own tool. Run the script with `--no-pr`.
   Then open the PR with the script's table as the body.
7. Do not run the bench.
