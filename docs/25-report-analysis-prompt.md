# Report analysis prompt

Use this prompt after a race where you dropped bookmarks. Give it to an AI coding agent
together with the zip from `pitwall report`. The agent turns each bookmark into an issue
writeup, a minimal fix and a regression test.

The bookmark and report workflow is in
[13-league-multiplayer.md](13-league-multiplayer.md), under *Test and feedback loop*.
In short:

1. During the race, long-press wheel button 2 or spacebar (≥ 800 ms) to drop a bookmark.
2. After the race, add a note to each bookmark on the feedback screen.
3. Run `pitwall recordings` to find the session's recording.
4. Run `pitwall report <recording>` to build the zip. `<recording>` is a path, a file
   name, a number from `pitwall recordings`, or `latest` (the default).

## What the zip contains

| File | Content |
|---|---|
| `session_<uid>_<start>.f1bin.zst` and `.f1idx` | the full recording and its lap index |
| `<mindset>.decisions.jsonl` | every fired, queued and suppressed call. The file appends across sessions, and `session_reset` records separate them. |
| `calls.json`, `grades.json`, `bookmarks.json` | the database rows for the latest session in the database |
| `config.yaml`, `system.json` | the resolved settings, config hash, session uid and pitwall version |

`pitwall report` does not trim the recording. The prompt tells the agent to cut a segment
around each bookmark with `pitwall trim`.

## How to use the prompt

1. Copy the code block below.
2. Replace `<session-id>`, `<path-to-report-zip>` and `<notes>`.
3. Give the prompt to the agent in a checkout of this repo.

For `<notes>`, paste the bookmark notes as one line per bookmark, for example
`L12 4:02 he's low on battery, but he is a friend`. If the notes are already in
`bookmarks.json`, write `see bookmarks.json`.

## Prompt template

````text
You are working in a checkout of the artsworks/f1-pitwall repository. Analyse a race
report bundle. Turn every bookmark in the bundle into an issue writeup, a minimal fix
and a regression test.

Inputs
- Session: <session-id>
- Report zip: <path-to-report-zip>
- Driver notes, one per bookmark: <notes>

Repository rules
- Do not commit recordings. *.f1bin, *.f1bin.zst and *.f1idx files stay out of git.
- Do not add code that starts, reads or inspects other processes. Pitwall only listens
  to UDP telemetry and writes under ~/.pitwall.
- Every feature must work with no LLM API key set.
- Do not edit the `expect` block of a scenario to make a test pass. Do not pass
  --accept-changes to `pitwall bench`. The repo owner approves both.
- Radio lines must give the driver information they can use, such as a gap, a battery
  level or a tyre difference. Say every number with one decimal.

Step 1. Unpack the bundle
1. Unzip <path-to-report-zip> into a scratch directory outside the repo.
2. Read system.json. Check that session_uid matches <session-id>. calls.json,
   grades.json and bookmarks.json hold rows for the latest session in the database, which
   can be a different session from the recording. If the uid does not match, use the
   bookmark records in the decision log instead and say so in the summary.
3. Run `uv run pitwall replay <recording> --no-rules --stats` for a packet census.
4. Split <mindset>.decisions.jsonl at its `session_reset` records. Keep only the part
   whose `bookmark` records have context.session_uid equal to <session-id>.

Step 2. Correlate each bookmark
For each bookmark (outcome "bookmark" in the decision log, or a row in bookmarks.json):
1. Note its lap, session_time, lap_distance, kind and context. The context is the
   snapshot at the press.
2. Match the bookmark to its note in <notes> by order, lap or time.
3. Cut a segment from about 60 s before to 15 s after the bookmark:
   `uv run pitwall trim <recording> --from-us <start> --to-us <end> --out <scratch>/bm<N>.f1bin`.
   Recording offsets are microseconds from the start of the recording.
4. Replay the segment: `uv run pitwall replay <scratch>/bm<N>.f1bin --seed-db :memory:`.
   If the session is online, also replay with --mask-restricted and compare the calls.
5. List every decision-log record in the same window. Include fired, queued and
   suppressed records, their rule_id, priority, suppressed_by, inputs and text.
6. Write down what the radio said and what it should have said.

Step 3. Classify each bookmark
Use exactly one type per bookmark:
- wrong call: the radio said something wrong for the situation.
- missed call: the radio stayed silent when it should have spoken.
- too chatty: the call was correct but repeated, too long or low value.
- bug: a crash, wrong data, a wrong name, a dashboard fault or anything that is not
  a rule decision.
- idea: no defect, a feature request.
The issue templates in .github/ISSUE_TEMPLATE use the title prefixes [wrong-call],
[missed-call] and [bug]. Use [too-chatty] and [idea] for the other two types.

Step 4. Online session audit
Do this step if the Session packet has m_networkGame = 1, or if Participants shows a
mix of m_aiControlled = 0 and 1 cars. Do it for every bookmark and once for the whole
session, even if no note mentions it.
1. Restricted telemetry. For each car, read m_yourTelemetry from Participants. 0 means
   Restricted. For Restricted cars, the game sends these fields as zero:
   - Car Status: fuel, fuel mix, ERS store and deploy, brake bias, engine power
   - Car Damage: tyre wear, tyre damage, wing damage, engine and gearbox wear
   Tyre compound and tyre age stay visible.
   Pitwall must treat these fields as unknown, never as zero. Check every rule, model
   and phrasing that reads rival fuel, ERS or wear. A call such as "he's low on
   battery" about a Restricted car is a bug. Pitwall must model Restricted cars from
   compound, tyre age, stint length and lap-time trend only.
   Gate on m_yourTelemetry per car, not on m_aiControlled. A slot can change between
   AI and human mid-session. Check how SessionState sets rival_data_restricted. Find
   out whether it works on a mixed grid, where only some rivals are Restricted.
2. Names. If m_showOnlineNames is 0 for a car, the game sends a placeholder name such
   as "Player". Two humans can have the same placeholder. The radio must then say
   "car ahead", the team name or the race number, or use the alias map in the league
   preset. A radio line that speaks the placeholder is a bug.
3. Disconnects. Check m_resultStatus in Lap Data and m_numActiveCars in Participants.
   When a car drops out, pitwall must remove it from battles, gaps and plans. The slot
   can be reused. Key each rival by slot and m_networkId, so that pitwall does not
   apply old state to a new car in the same slot.
4. Record each finding as a separate issue, even without a bookmark. Give the
   session_time and lap.

Step 5. Find the cause and fix it
1. Find the rule or module that made the decision. Rules are in
   src/pitwall/config/defaults/rules/*.yaml. Packet ingest and the snapshot are in
   src/pitwall/state/session.py. Masking is in src/pitwall/net/mask.py. Strategy and
   battles are in src/pitwall/strategy/.
2. Find the exact condition, threshold or missing field check that caused the output.
3. Make the smallest fix that corrects the decision. Prefer a rule or threshold change
   to new code. Do not change unrelated calls.
4. Run the changed rule check: `uv run pitwall rules check`.

Step 6. Add a regression test
1. Write a pytest test in tests/ that fails before the fix and passes after it. The
   test cannot read the recording, because recordings are not in git. Build the
   packet stream with the helpers in tests/synth.py from the values you read in the
   trimmed segment. For restricted cases, apply pitwall.net.mask.mask_restricted.
2. If the test replays packets through the engine, mark the module with
   `pytestmark = [pytest.mark.slow, pytest.mark.replay]`.
3. Also draft a scenario that replays the real recording. Put it in
   scenarios/templates/<session>-<short-name>.yaml with status: target. Copy the
   source block style (session_uid, sha256, where) from scenarios/brazil-penalty.yaml.
   The repo owner reviews the expect block and moves the file into scenarios/.
4. Run the new test and the fast tier:
   `uv run pytest <new test file>` and `uv run pytest -m "not slow" -n auto`.
5. Run `uv run ruff check . && uv run ruff format --check . && uv run mypy src`.

Step 7. Output
Write one issue per bookmark, plus one per online audit finding, in this format:

### [<type-prefix>] <short title>
- Bookmark: #<n>, lap <lap>, session time <mm:ss>, kind <kind>
- Driver note: <note, or "none">
- Online: <yes or no>. Cars involved: <slot, AI or human, m_yourTelemetry,
  m_networkId>
- Rule id: <rule_id, or "none">
- Expected radio: "<what the radio should have said, or silence>"
- Actual radio: "<what the radio said, or silence>". Include suppressed calls with
  their suppressed_by.
- Evidence: <decision-log records and snapshot values that prove the cause>
- Root cause: <file:line and the condition at fault>
- Fix: <one or two sentences, and the changed files>
- Test: <test file::test name. Show that it fails before the fix and passes after.>
- Scenario draft: <scenarios/templates/... path, or "none">

End with a summary table: bookmark, type, rule id, fixed (yes or no), test name.
List any bookmark you could not reproduce, with the reason.
````
