---
name: track-overlay-sync
description: Promote learned per-track values into the packaged overlays under src/pitwall/config/defaults/tracks/ when the repo owner supplies ~/.pitwall/tracks/*.yaml files or a learning-latest.json pack.
---

# Sync packaged track overlays from learned files

Use this procedure when the repo owner uploads `~/.pitwall/tracks/<id>.yaml` files, `learning-latest.json`, or both.
The goal is one PR that updates `src/pitwall/config/defaults/tracks/<id>.yaml`.

## Sources

- `~/.pitwall/tracks/<id>.yaml` is the output of `pitwall calibrate --write-overlay` (`calibrate.write_overlays`).
  These files contain only the learned keys, without `track_id` or `name`.
- The `track_overlays` block in `learning-latest.json` is a copy of those files, keyed by track id (`learnpack._overlays`).
- If both are supplied, the uploaded YAML files are authoritative.
  Check that each file matches its `track_overlays` entry and report any difference.
- Do not re-derive values from `model_params` or other raw pack data.
  Calibrate already applied the promotion gates.

## Rules

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

1. Compare each learned file with its packaged file. Print the keys that change.
2. Edit the packaged files. Keep the existing one-line flow style for maps.
3. Run this check. It prints `True` for each file when every learned key matches the packaged file.

   ```
   uv run python -c "
   import glob, os, yaml
   for f in sorted(glob.glob('<upload dir>/*.yaml')):
       u = yaml.safe_load(open(f)); p = yaml.safe_load(open('src/pitwall/config/defaults/tracks/' + os.path.basename(f)))
       print(os.path.basename(f), all(p.get(k) == v for k, v in u.items() if k != 'deg_ms_per_lap'))
   "
   ```

4. Run `uv run pytest -q tests/test_config.py tests/test_track_overlay.py tests/test_calibrate.py`.
   `test_track_thresholds_apply_only_after_selecting_track` checks the packaged Silverstone (track 7) fuel.
   If 7.yaml fuel changes, update that assertion to the new value.
5. Open one PR with a `feat(tracks):` title. List the old and new value for each changed key.
