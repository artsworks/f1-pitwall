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
    cmd_recordings,
    cmd_replay,
    cmd_report,
    cmd_stats,
    cmd_trim,
)
from pitwall.cli.serve import cmd_doctor, cmd_start
from pitwall.cli.update import cmd_update
from pitwall.cli.voice import cmd_voice
from pitwall.net.profile import PROFILES

EVERYDAY = (
    "start",
    "doctor",
    "recordings",
    "sessions",
    "replay",
    "stats",
    "digest",
    "debrief",
    "setup",
    "evaluate",
    "calibrate",
    "voice",
    "cleanup",
    "report",
    "update",
)
ADVANCED = ("diff", "bench", "rules", "propose", "tune", "restore", "trim")


def _cmd(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help: str,
    description: str,
    example: str,
) -> argparse.ArgumentParser:
    return sub.add_parser(
        name,
        help=help,
        description=description,
        epilog=f"e.g. {example}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def _command_lines(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], names: tuple[str, ...]
) -> str:
    help_by_name = {action.dest: action.help for action in sub._choices_actions}
    width = max(map(len, names))
    return "\n".join(f"  {name:<{width}}  {help_by_name[name]}" for name in names)


class _HelpAllAction(argparse.Action):
    def __init__(self, option_strings: list[str], dest: str, **kwargs: Any) -> None:
        self.full_epilog: str = kwargs.pop("full_epilog")
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        parser.epilog = self.full_epilog
        parser.print_help()
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pitwall",
        usage="%(prog)s [-h] [--help-all] [--log-level LEVEL] <command> ...",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="warning",
        help="console log level (default warning)",
    )
    sub = p.add_subparsers(
        dest="command",
        required=True,
        metavar="<command>",
        help=argparse.SUPPRESS,
        prog="pitwall",
    )

    rep = _cmd(
        sub,
        "replay",
        "replay a recording through the engine",
        "Rerun a recording through the engine to see the calls it makes.",
        "pitwall replay latest --speed 4 --serve",
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

    dif = _cmd(
        sub,
        "diff",
        "compare two rules dirs over recording(s)",
        "Compare two rule sets on the same recordings, call by call.",
        "pitwall diff latest --b my-rules/",
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

    tun = _cmd(
        sub,
        "tune",
        "fold review grades + A/B results into rule tuning",
        "Fold your call grades and A/B results into rule tuning.",
        "pitwall tune",
    )
    add_recording_arg(
        tun, "paths", multiple=True, help_prefix="recordings to ingest before tuning; "
    )
    add_calls_mode_arg(tun)
    add_db_arg(tun)
    tun.set_defaults(func=cmd_tune)

    cu = _cmd(
        sub,
        "cleanup",
        "delete old recordings, packs and caches (asks first)",
        "Reclaim disk space; keeps recordings not learned yet.",
        "pitwall cleanup --learn",
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
    cu.add_argument(
        "--learn",
        action="store_true",
        help="learn recordings old enough to delete, then plan the cleanup",
    )
    cu.set_defaults(func=cmd_cleanup)

    dg = _cmd(
        sub,
        "digest",
        "hindsight-grade a session and write its digest",
        "Learn from recordings: grades each session and writes its digest.",
        "pitwall digest recordings/",
    )
    add_recording_arg(dg, "paths", multiple=True, help_prefix="recordings to ingest; ")
    add_calls_mode_arg(dg)
    add_db_arg(dg)
    dg.add_argument("--session", default=None, help="session uid (default: latest)")
    dg.add_argument("--out", default=None, help="digest dir (default: ~/.pitwall/digests, - none)")
    add_json_arg(dg)
    dg.set_defaults(func=cmd_digest)

    debrief = _cmd(
        sub,
        "debrief",
        "export a standalone session debrief",
        "Export a session debrief as one HTML file you can open or share.",
        "pitwall debrief",
    )
    debrief.add_argument("--session", default="latest", help="session uid (default: latest)")
    add_db_arg(debrief)
    debrief.add_argument("--out", default=None, help="HTML output path")
    debrief.set_defaults(func=cmd_debrief)

    setup = _cmd(
        sub,
        "setup",
        "recommend setup changes for a recorded run",
        "Suggest car setup changes from how a recorded run went.",
        "pitwall setup --mode garage",
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

    ev = _cmd(
        sub,
        "evaluate",
        "compare recorded calls-on and calls-off outcomes",
        "Did the calls help? Compares calls-on vs calls-off outcomes.",
        "pitwall evaluate",
    )
    add_db_arg(ev)
    ev.add_argument("--track", type=int, default=None)
    add_json_arg(ev)
    ev.set_defaults(func=cmd_evaluate)

    proposal = _cmd(
        sub,
        "propose",
        "review-only corpus threshold proposals",
        "Suggest rule threshold changes from all sessions, for you to review.",
        "pitwall propose",
    )
    add_db_arg(proposal)
    proposal.add_argument("--candidate-rules", type=Path, default=None)
    proposal.add_argument("--out", default=None)
    proposal.set_defaults(func=cmd_propose)

    cal = _cmd(
        sub,
        "calibrate",
        "fit track priors from recorded sessions",
        "Fit track priors for pace, tyre wear and fuel from your sessions.",
        "pitwall calibrate --track 7 --dry-run",
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

    rpt = _cmd(
        sub,
        "report",
        "bundle a recording + decisions for a bug report",
        "Bundle a recording and its decision log into a zip for a bug report.",
        "pitwall report latest -o report.zip",
    )
    add_recording_arg(rpt, "recording")
    rpt.add_argument("--recording", dest="recording_opt", default=None, help=argparse.SUPPRESS)
    rpt.add_argument("-o", "--out", default=None, help="output zip path")
    rpt.set_defaults(func=cmd_report)

    rc = _cmd(
        sub,
        "rules",
        "rule tooling",
        "Tools for rule authors.",
        "pitwall rules check",
    )
    rsub = rc.add_subparsers(dest="rules_command", required=True)
    rcheck = _cmd(
        rsub,
        "check",
        "validate rule expressions",
        "Find broken rule expressions before a session.",
        "pitwall rules check",
    )
    rcheck.set_defaults(func=cmd_rules_check)

    bench = _cmd(
        sub,
        "bench",
        "replay scenarios and compare scorecards",
        "Replay scenario files and check the calls against a baseline scorecard.",
        "pitwall bench --only brazil-sc-early",
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

    st = _cmd(
        sub,
        "stats",
        "packet census of a recording",
        "Show what a recording holds, or what Pitwall has learned so far.",
        "pitwall stats --learned",
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

    restore = _cmd(
        sub,
        "restore",
        "restore learned state from a learning pack",
        "Restore learned state from a learning pack, for example on a new PC.",
        "pitwall restore learnings/learning-latest.json",
    )
    restore.add_argument("pack", type=Path)
    add_db_arg(restore)
    restore.add_argument("--overlay-dir", type=Path, default=None)
    restore.set_defaults(func=cmd_restore)

    vc = _cmd(
        sub,
        "voice",
        "Piper and OmniVoice speech, voice checks, and voice-command channel",
        "Manage speech: list and download voices, pre-render phrases, test audio.",
        'pitwall voice say "Box this lap"',
    )
    vc.set_defaults(func=cmd_voice, voice_action=None)
    vsub = vc.add_subparsers(dest="voice_action")
    _cmd(
        vsub,
        "list",
        "list installed and suggested Piper voices (default)",
        "Show installed Piper voices and suggested ones to download.",
        "pitwall voice list",
    )
    vget = _cmd(
        vsub,
        "get",
        "download Piper voices",
        "Download Piper voices, by default the configured one.",
        "pitwall voice get en_GB-alan-medium",
    )
    vget.add_argument("names", nargs="*", help="voice names (default: configured)")
    warm = _cmd(
        vsub,
        "warm",
        "pre-render fixed phrases with Piper or OmniVoice, optionally from a recording",
        "Pre-render common phrases so the first calls play without delay.",
        "pitwall voice warm --engine omnivoice --replay latest",
    )
    warm.add_argument("--engine", choices=["auto", "piper", "omnivoice"], default="auto")
    warm.add_argument(
        "--replay",
        default=None,
        help="recording: latest, an index, a name or a path",
    )
    vsay = _cmd(
        vsub,
        "say",
        "audio check: speak a line through the speech backend",
        "Speak a test line to check audio output and the voice.",
        'pitwall voice say "Box this lap"',
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
    _cmd(
        vsub,
        "devices",
        "list SAPI recognisers and audio inputs",
        "List speech recognisers and audio inputs for voice commands.",
        "pitwall voice devices",
    )
    vgram = _cmd(
        vsub,
        "grammar",
        "print the SRGS grammar",
        "Print the voice-command grammar the recogniser listens for.",
        "pitwall voice grammar --lang en-GB",
    )
    vgram.add_argument("--lang", default="en-US", help="xml:lang of the grammar")
    vspike = _cmd(
        vsub,
        "spike",
        "Phase 0 SAPI recogniser check",
        "Test voice commands live with the SAPI recogniser.",
        "pitwall voice spike --device 1 --say",
    )
    vspike.add_argument("--device", type=int, default=None, help="audio input index")
    vspike.add_argument("--recognizer", default=None, help="recogniser description substring")
    vspike.add_argument("--confidence", type=float, default=None, help="confidence_min override")
    vspike.add_argument("--affinity", default=None, help="CPU affinity mask, e.g. 0xF000")
    vspike.add_argument(
        "--say", action="store_true", help="speak 'Copy, <intent>' through speech.engine"
    )
    vspike.add_argument("--log", default=None, help="JSONL log path")

    rl = _cmd(
        sub,
        "recordings",
        "list recordings with their # for other commands",
        "List recordings newest first, with the # that other commands accept.",
        "pitwall recordings",
    )
    rl.set_defaults(func=cmd_recordings)

    ss = _cmd(
        sub,
        "sessions",
        "list stored sessions, newest first",
        "List stored sessions newest first, to find a session uid.",
        "pitwall sessions --limit 5",
    )
    add_db_arg(ss)
    ss.add_argument("--limit", type=int, default=20)
    ss.set_defaults(func=cmd_sessions)

    doc = _cmd(
        sub,
        "doctor",
        "bind-test the port and report observed telemetry",
        "Check your setup: tests the UDP port and shows which telemetry arrives.",
        "pitwall doctor --seconds 10",
    )
    doc.add_argument("--host", default="0.0.0.0")
    doc.add_argument("--port", type=int, default=20777)
    doc.add_argument("--seconds", type=float, default=5.0)
    doc.set_defaults(func=cmd_doctor)

    st2 = _cmd(
        sub,
        "start",
        "live: UDP ingest + rules + dashboard + speech",
        "Run live: listens to game telemetry, makes calls, speaks them.",
        "pitwall start",
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

    trim = _cmd(
        sub,
        "trim",
        "extract a time range from a recording",
        "Cut a time range or downsample a recording, for example full to lite.",
        "pitwall trim latest --profile lite --out lite.f1bin",
    )
    add_recording_arg(trim)
    trim.add_argument("--from-us", type=int, default=None)
    trim.add_argument("--to-us", type=int, default=None)
    trim.add_argument(
        "--profile", choices=PROFILES, default=None, help="also downsample (e.g. full -> lite)"
    )
    trim.add_argument("--out", required=True)
    trim.set_defaults(func=cmd_trim)

    upd = _cmd(
        sub,
        "update",
        "update pitwall: git pull, then uv sync",
        "Update Pitwall: pull the latest code with git, then sync dependencies with uv.",
        "pitwall update",
    )
    upd.set_defaults(func=cmd_update)

    everyday_epilog = (
        "Everyday commands:\n"
        f"{_command_lines(sub, EVERYDAY)}\n\n"
        "Advanced commands for rule-engine work: diff, bench, rules, propose, tune, restore, trim. "
        "Run pitwall --help-all to list them.\n"
        "Run pitwall <command> -h for an example."
    )
    full_epilog = (
        "Everyday commands:\n"
        f"{_command_lines(sub, EVERYDAY)}\n\n"
        "Advanced commands:\n"
        f"{_command_lines(sub, ADVANCED)}\n\n"
        "Run pitwall <command> -h for an example."
    )
    p.epilog = everyday_epilog
    p.add_argument(
        "--help-all",
        action=_HelpAllAction,
        full_epilog=full_epilog,
        help="list advanced commands too",
    )
    return p
