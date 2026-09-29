---
name: running-tests
description: Run pitwall checks efficiently - targeted tests while iterating, the full suite once before pushing.
---

# Running tests

The full suite takes about 11 minutes, mostly replay and supervisor tests.
Do not run it after every edit.

## While iterating

Run only the test files for the code you changed, then fast checks:

```bash
uv run pytest -q tests/test_state.py        # affected files only
uv run pytest -q --lf                       # re-run last failures
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
node --check web/app.js web/review.js       # after web/ changes
```

Common mappings: `state/session.py` -> `test_state.py`, `test_race_state.py`;
`engine.py` -> `test_engine_persistence.py`, `test_engine_replay.py`;
`hindsight.py` -> `test_hindsight.py`; `strategy/plans.py` -> `test_plans.py`,
`test_plan_replay.py`; `store/db.py` -> `test_db.py`, `test_db_m3.py`.

## Before pushing

Run the full suite once: `uv run pytest -q`. Run it in the background and poll.

`tests/test_supervisor.py::test_crashing_engine_is_restarted_and_recording_continues`
is timing-sensitive and can fail on a slow VM. Report it as failing; do not
call it green or edit it to pass.

## Real recordings

To check a logic change against a user recording, replay it and diff fired calls
in `recordings/decisions.jsonl` before and after:

```bash
uv run pitwall replay /path/session.f1bin.zst --speed max
```

Delete the old `recordings/` output first; the decision log appends.
