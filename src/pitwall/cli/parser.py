from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pitwall.cli.common import (
    add_calls_mode_arg,
    add_db_arg,
    add_json_arg,
    add_recording_arg,
)
from pitwall.cli.learn import (
    _positive_int,
    cmd_bench,
    cmd_calibrate,
    cmd_debrief,
    cmd_diff,
    cmd_digest,
    cmd_evaluate,
    cmd_propose,
    cmd_restore,
    cmd_rules_check,
    cmd_sessions,
    cmd_setup,
    cmd_tune,
)
from pitwall.cli.recordings import (
    cmd_cleanup,
    cmd_derive,
    cmd_recordings,
    cmd_replay,
    cmd_report,
    cmd_stats,
    cmd_trim,
)
from pitwall.cli.serve import cmd_doctor, cmd_start
from pitwall.cli.voice import cmd_voice
from pitwall.net.profile import PROFILES


def _doc(benefit: str, example: str) -> dict[str, Any]:
    return {
        "description": benefit,
        "epilog": f"example:\n  {example}",
        "formatter_class": argparse.RawDescriptionHelpFormatter,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pitwall")
    p.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="warning",
        help="console log level (default warning)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    rep = sub.add_parser(
        "replay",
        **_doc(
            "Test calls on a past race without driving: plays a recording through the engine.",
            "pitwall replay latest --speed 10 --stats",
        ),
        help="replay a recording through the engine",
    )
    add_recording_arg(rep)
    rep.add_argument("--speed", default="max", help="1|N|max")
    rep.add_argument("--stats", action="store_true")
    rep.add_argument("--no-rules", action="store_true", help="packet census only")
    rep.add_argument(
        "--from-lap", type=int, default=None, help="seek via .f1idx (rebuilt if missing or stale)"
    )
    rep.add_argument("--from-us", type=int, default=None)
    rep.add_argument("--to-us", type=int, default=None)
    rep.add_argument("--serve", action="store_true", help="run dashboard while replaying")
    rep.add_argument(
        "--mask-restricted",
        action="store_true",
        help="zero rival fuel/ERS/tyre-wear fields, as an online lobby with restricted telemetry",
    )
    rep.add_argument(
        "--seed-db",
        default=":memory:",
        help="SQLite db for replay persistence (default :memory:; never the real DB)",
    )
    rep.set_defaults(func=cmd_replay)

    dif = sub.add_parser(
        "diff",
        **_doc(
            "See how a rules change alters calls before you use it live.",
            "pitwall diff latest --b my-rules",
        ),
        help="compare two rules dirs over recording(s)",
    )
    add_recording_arg(dif, "recordings", multiple=True, required=True)
    dif.add_argument("--a", default=None, help="rules dir A (default: packaged defaults)")
    dif.add_argument("--b", required=True, help="rules dir B")
    dif.add_argument("--a-mindset", default=None)
    dif.add_argument("--b-mindset", default=None)
    dif.add_argument("--corpus", default=None, help="glob of extra recordings to aggregate")
    add_json_arg(dif)
    dif.add_argument(
        "--record", action="store_true", help="store per-rule A/B counts in SQLite for tune"
    )
    dif.set_defaults(func=cmd_diff)

    tun = sub.add_parser(
        "tune",
        **_doc("Turn your review grades and A/B results into rule tuning.", "pitwall tune"),
        help="fold review grades + A/B results into rule tuning",
    )
    add_recording_arg(
        tun, "paths", multiple=True, help_prefix="recordings to ingest before tuning; "
    )
    add_calls_mode_arg(tun)
    add_db_arg(tun)
    tun.set_defaults(func=cmd_tune)

    cu = sub.add_parser(
        "cleanup",
        **_doc("Reclaim disk space; keeps recordings not learned yet.", "pitwall cleanup --days 7"),
        help="delete old recordings, packs and caches (asks first)",
    )
    cu.add_argument(
        "--days", type=float, default=5.0, help="only files older than this (default: 5)"
    )
    cu.add_argument("--recordings", default=None, help="recordings folder (default: settings)")
    add_db_arg(cu)
    cu.add_argument("--yes", action="store_true", help="delete without asking")
    cu.add_argument(
        "--include-unlearned",
        action="store_true",
        help="also delete old recordings not learned yet",
    )
    cu.set_defaults(func=cmd_cleanup)

    dg = sub.add_parser(
        "digest",
        **_doc("See which calls helped: grades the session in hindsight.", "pitwall digest"),
        help="hindsight-grade a session and write its digest",
    )
    add_recording_arg(dg, "paths", multiple=True, help_prefix="recordings to ingest; ")
    add_calls_mode_arg(dg)
    add_db_arg(dg)
    dg.add_argument("--session", default=None, help="session uid (default: latest)")
    dg.add_argument("--out", default=None, help="digest dir (default: ~/.pitwall/digests, - none)")
    add_json_arg(dg)
    dg.set_defaults(func=cmd_digest)

    debrief = sub.add_parser(
        "debrief",
        **_doc("Review or share a session as one HTML page.", "pitwall debrief"),
        help="export a standalone session debrief",
    )
    debrief.add_argument("--session", default="latest", help="session uid (default: latest)")
    add_db_arg(debrief)
    debrief.add_argument("--out", default=None, help="HTML output path")
    debrief.set_defaults(func=cmd_debrief)

    setup = sub.add_parser(
        "setup",
        **_doc(
            "Get car setup changes based on how your last run went.", "pitwall setup --mode garage"
        ),
        help="recommend setup changes for a recorded run",
    )
    setup.add_argument("session", nargs="?", default=None, help="session uid (default: latest)")
    setup.add_argument(
        "--mode",
        choices=["debrief", "garage"],
        default="debrief",
        help="evaluation context (default: debrief)",
    )
    add_db_arg(setup)
    add_json_arg(setup, help="print recommendation JSON")
    setup.add_argument("--store", action="store_true", help="store recommendations in SQLite")
    setup.set_defaults(func=cmd_setup)

    ev = sub.add_parser(
        "evaluate",
        **_doc("Did the calls help? Compares calls-on vs calls-off outcomes.", "pitwall evaluate"),
        help="compare recorded calls-on and calls-off outcomes",
    )
    add_db_arg(ev)
    ev.add_argument("--track", type=int, default=None)
    add_json_arg(ev)
    ev.set_defaults(func=cmd_evaluate)

    proposal = sub.add_parser(
        "propose",
        **_doc(
            "Get rule threshold suggestions from all your sessions. Changes no files.",
            "pitwall propose --out proposals.yaml",
        ),
        help="review-only corpus threshold proposals",
    )
    add_db_arg(proposal)
    proposal.add_argument("--candidate-rules", type=Path, default=None)
    proposal.add_argument("--out", default=None)
    proposal.set_defaults(func=cmd_propose)

    cal = sub.add_parser(
        "calibrate",
        **_doc(
            "Match pace, wear and pit-loss estimates to your driving on each track.",
            "pitwall calibrate --dry-run",
        ),
        help="fit track priors from recorded sessions",
    )
    add_recording_arg(
        cal, "paths", multiple=True, help_prefix="recordings to ingest before calibration; "
    )
    add_db_arg(cal)
    cal.add_argument("--track", type=int, default=None)
    cal.add_argument("--dry-run", action="store_true")
    cal.add_argument("--include-synthetic", action="store_true")
    cal.add_argument("--write-overlay", action="store_true")
    cal.add_argument("--overlay-dir", type=Path, default=None)
    add_json_arg(cal)
    cal.set_defaults(func=cmd_calibrate)

    rpt = sub.add_parser(
        "report",
        **_doc(
            "File a bug with one zip of the recording and its decision log.",
            "pitwall report latest -o bug.zip",
        ),
        help="bundle a recording + decisions for a bug report",
    )
    add_recording_arg(rpt, "recording")
    rpt.add_argument("--recording", dest="recording_opt", default=None, help=argparse.SUPPRESS)
    rpt.add_argument("-o", "--out", default=None, help="output zip path")
    rpt.set_defaults(func=cmd_report)

    rc = sub.add_parser(
        "rules",
        **_doc("Catch rule mistakes before a race.", "pitwall rules check"),
        help="rule tooling",
    )
    rsub = rc.add_subparsers(dest="rules_command", required=True)
    rcheck = rsub.add_parser(
        "check",
        **_doc("Find broken rule expressions before they fail live.", "pitwall rules check"),
        help="validate rule expressions",
    )
    rcheck.set_defaults(func=cmd_rules_check)

    trim = sub.add_parser("trim", help="extract a time range from a recording")
    add_recording_arg(trim)
    trim.add_argument("--from-us", type=int, default=None)
    trim.add_argument("--to-us", type=int, default=None)
    trim.add_argument(
        "--profile", choices=PROFILES, default=None, help="also downsample (e.g. full -> lite)"
    )
    trim.add_argument("--out", required=True)
    trim.set_defaults(func=cmd_trim)

    derive = sub.add_parser("derive", help="derive a synthetic recording from a real session")
    add_recording_arg(derive, required=True)
    derive.add_argument("out", help="output .f1bin or .f1bin.zst path")
    derive.add_argument("--wear-scale", type=float, default=None)
    derive.add_argument("--inject-sc", metavar="START[-END]", default=None)
    derive.add_argument("--vsc", action="store_true")
    derive.add_argument("--penalty", type=int, default=None, metavar="LAP")
    derive.set_defaults(func=cmd_derive)

    bench = sub.add_parser(
        "bench",
        **_doc(
            "Catch call regressions: replays scenarios and compares scorecards to the baseline.",
            "pitwall bench",
        ),
        help="replay scenarios and compare scorecards",
    )
    bench.add_argument("--scenarios", default="scenarios", help="scenario directory")
    bench.add_argument(
        "--recordings",
        action="append",
        default=None,
        metavar="DIR",
        help="recording directory to search (repeatable)",
    )
    bench.add_argument("--rules", type=Path, default=None, help="rules directory")
    bench.add_argument("--only", nargs="+", default=None, metavar="ID")
    bench.add_argument("--baseline", type=Path, default=None, help="baseline scorecard path")
    bench.add_argument("--tolerance", type=float, default=0.02)
    bench.add_argument("--update-baseline", action="store_true")
    bench.add_argument(
        "--accept-changes",
        action="store_true",
        help="with --update-baseline, accept removed or edited passing checks",
    )
    bench.add_argument("--note", default=None, help="note for a baseline update")
    bench.add_argument("--out", type=Path, default=None, help="write scorecard JSON")
    add_json_arg(bench, help="print scorecard and gate as JSON")
    bench.add_argument("--trend", action="store_true", help="show history trend without replay")
    bench.add_argument("--window", type=_positive_int, default=5)
    bench.set_defaults(func=cmd_bench)

    st = sub.add_parser(
        "stats",
        **_doc(
            "See what a recording contains, or what pitwall has learned.", "pitwall stats latest"
        ),
        help="packet census of a recording",
    )
    add_recording_arg(st)
    stats_modes = st.add_mutually_exclusive_group()
    stats_modes.add_argument("--learned", action="store_true")
    stats_modes.add_argument("--quality", action="store_true")
    st.add_argument("--sessions", type=int, default=10)
    add_db_arg(st)
    st.add_argument("--track", type=int, default=None)
    add_json_arg(st)
    st.set_defaults(func=cmd_stats)

    restore = sub.add_parser(
        "restore",
        **_doc(
            "Move learned state to a new PC, or recover it from a learning pack.",
            "pitwall restore learning-latest.json",
        ),
        help="restore learned state from a learning pack",
    )
    restore.add_argument("pack", type=Path)
    add_db_arg(restore)
    restore.add_argument("--overlay-dir", type=Path, default=None)
    restore.set_defaults(func=cmd_restore)

    vc = sub.add_parser(
        "voice",
        **_doc("Set up and test the radio voice and voice commands.", "pitwall voice say"),
        help="Piper and OmniVoice speech, voice checks, and voice-command channel",
    )
    vc.set_defaults(func=cmd_voice, voice_action=None)
    vsub = vc.add_subparsers(dest="voice_action")
    vsub.add_parser(
        "list",
        **_doc("See which Piper voices you have and which to get.", "pitwall voice list"),
        help="list installed and suggested Piper voices (default)",
    )
    vget = vsub.add_parser(
        "get",
        **_doc(
            "Download Piper voices so calls sound natural.", "pitwall voice get en_GB-alan-medium"
        ),
        help="download Piper voices",
    )
    vget.add_argument("names", nargs="*", help="voice names (default: configured)")
    warm = vsub.add_parser(
        "warm",
        **_doc(
            "Cut first-call delay by rendering fixed phrases ahead of time.",
            "pitwall voice warm --engine omnivoice",
        ),
        help="pre-render fixed phrases with Piper or OmniVoice, optionally from a recording",
    )
    warm.add_argument("--engine", choices=["auto", "piper", "omnivoice"], default="auto")
    warm.add_argument(
        "--replay",
        default=None,
        help="recording: latest, an index, a name or a path",
    )
    vsay = vsub.add_parser(
        "say",
        **_doc("Check speech works before you drive.", 'pitwall voice say "Box this lap"'),
        help="audio check: speak a line through the speech backend",
    )
    vsay.add_argument("text", nargs="?", default="Pit wall online. Radio check.")
    vsay.add_argument(
        "--engine",
        choices=["auto", "piper", "null", "omnivoice"],
        default=None,
        help="speech backend (default: configured speech.engine)",
    )
    vsay.add_argument("--voice", help="Piper voice name, e.g. en_GB-alan-medium")
    vsay.add_argument("--speed", type=float, help="Piper pace multiplier (>1 faster)")
    vsay.add_argument("--save", metavar="WAV", help="render with Piper to a WAV file instead")
    vsub.add_parser(
        "devices",
        **_doc("Find the mic index and recogniser for voice commands.", "pitwall voice devices"),
        help="list SAPI recognisers and audio inputs",
    )
    vgram = vsub.add_parser(
        "grammar",
        **_doc("See which phrases voice commands understand.", "pitwall voice grammar"),
        help="print the SRGS grammar",
    )
    vgram.add_argument("--lang", default="en-US", help="xml:lang of the grammar")
    vspike = vsub.add_parser(
        "spike",
        **_doc(
            "Test voice recognition on your mic before a race.", "pitwall voice spike --device 1"
        ),
        help="Phase 0 SAPI recogniser check",
    )
    vspike.add_argument("--device", type=int, default=None, help="audio input index")
    vspike.add_argument("--recognizer", default=None, help="recogniser description substring")
    vspike.add_argument("--confidence", type=float, default=None, help="confidence_min override")
    vspike.add_argument("--affinity", default=None, help="CPU affinity mask, e.g. 0xF000")
    vspike.add_argument(
        "--say", action="store_true", help="speak 'Copy, <intent>' through speech.engine"
    )
    vspike.add_argument("--log", default=None, help="JSONL log path")

    rl = sub.add_parser(
        "recordings",
        **_doc("Find a recording's # to pass to other commands.", "pitwall recordings"),
        help="list recordings with their # for other commands",
    )
    rl.set_defaults(func=cmd_recordings)

    ss = sub.add_parser(
        "sessions",
        **_doc("Find a session uid for digest, debrief or setup.", "pitwall sessions --limit 5"),
        help="list stored sessions, newest first",
    )
    add_db_arg(ss)
    ss.add_argument("--limit", type=int, default=20)
    ss.set_defaults(func=cmd_sessions)

    doc = sub.add_parser(
        "doctor",
        **_doc(
            "Find why the dashboard is empty: checks the port and the telemetry the game sends.",
            "pitwall doctor --seconds 30",
        ),
        help="bind-test the port and report observed telemetry",
    )
    doc.add_argument("--host", default="0.0.0.0")
    doc.add_argument("--port", type=int, default=20777)
    doc.add_argument("--seconds", type=float, default=5.0)
    doc.set_defaults(func=cmd_doctor)

    st2 = sub.add_parser(
        "start",
        **_doc("Run live: listens to game telemetry, makes calls, speaks them.", "pitwall start"),
        help="live: UDP ingest + rules + dashboard + speech",
    )
    st2.add_argument(
        "--record",
        choices=[*PROFILES, "off"],
        default=None,
        help="recording profile (default: settings recording.profile = lite); "
        "full = every packet at native rate, for debugging/tuning",
    )
    st2.add_argument(
        "--no-watchdog",
        action="store_true",
        help="run recorder and engine in one process (no crash restart)",
    )
    st2.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    st2.set_defaults(func=cmd_start)

    return p
