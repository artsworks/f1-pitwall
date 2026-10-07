# Local run

Use a Windows PC with the game closed before running offline analysis.

## Install

Run the CPU installation:

```powershell
uv sync
```

Run the CUDA installation when you want GPU rollouts:

```powershell
uv sync --extra gpu
```

Check that Torch can access the GPU:

```powershell
uv run python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

## Close the game

Close the game before you run pitwall. The offline commands need no game session.
The live engine never inspects or spawns processes because of anti-cheat constraints.

## Calibrate priors

Calibrate the database after you record real races:

```powershell
uv run pitwall calibrate --track <TRACK_ID>
```

Calibration runtime: fill in after the first run.

## Generate a corpus

Generate eight races with fourteen workers on a sixteen-thread PC:

```powershell
uv run pitwall generate --track <TRACK_ID> --laps <LAPS> --seed 1 --races 8 --jobs 14 --random-sc --priors-db "$env:USERPROFILE\.pitwall\pitwall.sqlite" --out .\recordings\synthetic
```

Corpus runtime: fill in after the first run.

## Fill and approve templates

Run the `source.where` command in each template.
Replace `<recording-directory>/synthetic` with `.\recordings\synthetic`.
Copy each command's UID and SHA-256 into its matching template.

Review every expected rule and lap range against the generated recording.

Ask the repo owner to approve each template before moving it into `scenarios/`.

## Run the bench

Run the scenarios against the generated corpus:

```powershell
uv run pitwall bench --jobs 14 --scenarios .\scenarios --recordings .\recordings
```

Bench runtime: fill in after the first run.

## Run CUDA rollouts

Run two hundred thousand simulations on the GPU:

```powershell
uv run pitwall rollout --track <TRACK_ID> --laps <LAPS> --sims 200000 --device cuda --priors-db "$env:USERPROFILE\.pitwall\pitwall.sqlite"
```

Rollout runtime: fill in after the first run.

## Run the bounded training loop

Limit each search and save its report:

```powershell
uv run python scripts/train_loop.py --target <SCENARIO_ID> --param free_stop_margin_s=0,1 --param pit_min_laps_left=2,3 --max-iters 3 --max-minutes 10 --jobs 14 --device cuda --sims 200000 --out .\train-report.json
```

Training runtime: fill in after the first run.

## Update the baseline

Do not pass `--accept-changes`.
If the gate lists `changed` items, the run stops until the repo owner approves the expectation change.
Update the baseline only after the gate passes and the repo owner approves the change.
Review every rule change found only on synthetic races against real recordings.

```powershell
uv run pitwall bench --jobs 14 --scenarios .\scenarios --recordings .\recordings --update-baseline --note "Reviewed local run"
```

Do not update the baseline after a failed or incomplete gate.

## Results for the PR

Paste these results into the PR:

- PC model and GPU model.
- Calibration runtime and track IDs.
- Corpus runtime, worker count, and generated recording count.
- Bench gate, scorecard path, and runtime.
- Rollout runtime, simulation count, device, and mean finish position.
- Training stop reason, winner parameters, and report path.
- Any rule changes that passed on real recordings.
