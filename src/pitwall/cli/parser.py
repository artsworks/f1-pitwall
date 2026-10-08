from __future__ import annotations

import argparse
from pathlib import Path

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pitwall")
    sub = p.add_subparsers(dest="command", required=True)

    rep = sub.add_parser("replay", help="replay a recording through the engine")
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

    dif = sub.add_parser("diff", help="compare two rules dirs over recording(s)")
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

    tun = sub.add_parser("tune", help="fold review grades + A/B results into rule tuning")
    add_recording_arg(
        tun, "paths", multiple=True, help_prefix="recordings to ingest before tuning; "
    )
    add_calls_mode_arg(tun)
    add_db_arg(tun)
    tun.set_defaults(func=cmd_tune)

    cu = sub.add_parser("cleanup", help="delete old learned recordings and caches (asks first)")
    cu.add_argument("--days", type=float, default=30.0, help="only files older than this")
    cu.add_argument("--recordings", default=None, help="recordings folder (default: settings)")
    add_db_arg(cu)
    cu.add_argument("--yes", action="store_true", help="delete without asking")
    cu.set_defaults(func=cmd_cleanup)

    dg = sub.add_parser("digest", help="hindsight-grade a session and write its digest")
    add_recording_arg(dg, "paths", multiple=True, help_prefix="recordings to ingest; ")
    add_calls_mode_arg(dg)
    add_db_arg(dg)
    dg.add_argument("--session", default=None, help="session uid (default: latest)")
    dg.add_argument("--out", default=None, help="digest dir (default: ~/.pitwall/digests, - none)")
    add_json_arg(dg)
    dg.set_defaults(func=cmd_digest)

    debrief = sub.add_parser("debrief", help="export a standalone session debrief")
    debrief.add_argument("--session", default="latest", help="session uid (default: latest)")
    add_db_arg(debrief)
    debrief.add_argument("--out", default=None, help="HTML output path")
    debrief.set_defaults(func=cmd_debrief)

    setup = sub.add_parser("setup", help="recommend setup changes for a recorded run")
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

    ev = sub.add_parser("evaluate", help="compare recorded calls-on and calls-off outcomes")
    add_db_arg(ev)
    ev.add_argument("--track", type=int, default=None)
    add_json_arg(ev)
    ev.set_defaults(func=cmd_evaluate)

    proposal = sub.add_parser("propose", help="review-only corpus threshold proposals")
    add_db_arg(proposal)
    proposal.add_argument("--candidate-rules", type=Path, default=None)
    proposal.add_argument("--out", default=None)
    proposal.set_defaults(func=cmd_propose)

    cal = sub.add_parser("calibrate", help="fit track priors from recorded sessions")
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

    rpt = sub.add_parser("report", help="bundle a recording + decisions for a bug report")
    add_recording_arg(rpt, "recording")
    rpt.add_argument("--recording", dest="recording_opt", default=None, help=argparse.SUPPRESS)
    rpt.add_argument("-o", "--out", default=None, help="output zip path")
    rpt.set_defaults(func=cmd_report)

    rc = sub.add_parser("rules", help="rule tooling")
    rsub = rc.add_subparsers(dest="rules_command", required=True)
    rcheck = rsub.add_parser("check", help="validate rule expressions")
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

    bench = sub.add_parser("bench", help="replay scenarios and compare scorecards")
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

    st = sub.add_parser("stats", help="packet census of a recording")
    add_recording_arg(st)
    stats_modes = st.add_mutually_exclusive_group()
    stats_modes.add_argument("--learned", action="store_true")
    stats_modes.add_argument("--quality", action="store_true")
    st.add_argument("--sessions", type=int, default=10)
    add_db_arg(st)
    st.add_argument("--track", type=int, default=None)
    add_json_arg(st)
    st.set_defaults(func=cmd_stats)

    restore = sub.add_parser("restore", help="restore learned state from a learning pack")
    restore.add_argument("pack", type=Path)
    add_db_arg(restore)
    restore.add_argument("--overlay-dir", type=Path, default=None)
    restore.set_defaults(func=cmd_restore)

    vc = sub.add_parser("voice", help="Piper voices, speech check, voice-command channel")
    vc.set_defaults(func=cmd_voice, voice_action=None)
    vsub = vc.add_subparsers(dest="voice_action")
    vsub.add_parser("list", help="list installed and suggested Piper voices (default)")
    vget = vsub.add_parser("get", help="download Piper voices")
    vget.add_argument("names", nargs="*", help="voice names (default: configured)")
    vsub.add_parser("warm", help="pre-render common fixed phrases")
    vsay = vsub.add_parser("say", help="audio check: speak a line through the speech backend")
    vsay.add_argument("text", nargs="?", default="Pit wall online. Radio check.")
    vsay.add_argument("--engine", choices=["auto", "piper", "sapi", "null"], default="auto")
    vsay.add_argument("--voice", help="Piper voice name, e.g. en_GB-alan-medium")
    vsay.add_argument("--speed", type=float, help="Piper pace multiplier (>1 faster)")
    vsay.add_argument("--save", metavar="WAV", help="render with Piper to a WAV file instead")
    vsub.add_parser("devices", help="list SAPI recognisers and audio inputs")
    vgram = vsub.add_parser("grammar", help="print the SRGS grammar")
    vgram.add_argument("--lang", default="en-US", help="xml:lang of the grammar")
    vspike = vsub.add_parser("spike", help="Phase 0 SAPI recogniser check")
    vspike.add_argument("--port", type=int, default=None, help="UDP port for Action 1 taps")
    vspike.add_argument("--no-udp", action="store_true", help="Enter key only; don't bind UDP")
    vspike.add_argument("--device", type=int, default=None, help="audio input index")
    vspike.add_argument("--recognizer", default=None, help="recogniser description substring")
    vspike.add_argument("--confidence", type=float, default=None, help="confidence_min override")
    vspike.add_argument("--affinity", default=None, help="CPU affinity mask, e.g. 0xF000")
    vspike.add_argument("--grammar", choices=["srgs", "api"], default="srgs")
    vspike.add_argument("--say", action="store_true", help="speak 'Copy, <intent>' via SAPI")
    vspike.add_argument("--log", default=None, help="JSONL log path")

    rl = sub.add_parser("recordings", help="list recordings with their # for other commands")
    rl.set_defaults(func=cmd_recordings)

    ss = sub.add_parser("sessions", help="list stored sessions, newest first")
    add_db_arg(ss)
    ss.add_argument("--limit", type=int, default=20)
    ss.set_defaults(func=cmd_sessions)

    doc = sub.add_parser("doctor", help="bind-test the port and report observed telemetry")
    doc.add_argument("--host", default="0.0.0.0")
    doc.add_argument("--port", type=int, default=20777)
    doc.add_argument("--seconds", type=float, default=5.0)
    doc.set_defaults(func=cmd_doctor)

    st2 = sub.add_parser("start", help="live: UDP ingest + rules + dashboard + speech")
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
