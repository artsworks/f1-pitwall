"""Deterministic, standalone post-session review built from persisted evidence."""

from __future__ import annotations

import hashlib
import html
import json
import os
import statistics
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

from pitwall.config.loader import config_hash
from pitwall.config.models import Settings
from pitwall.derive import is_synthetic_uid
from pitwall.hindsight import stop_laps
from pitwall.learned import learned_state
from pitwall.model.deg import fuel_burned_laps
from pitwall.protocol.enums import session_kind
from pitwall.setup.advisor import recommend_for_session
from pitwall.setup.evaluate import explain
from pitwall.state.session import thermal_window
from pitwall.store.db import Database, LapRow, PitEventRow, StintRow

_STYLE = """<style>
:root {
  --bg:#0a0a0c; --panel:#121216; --panel2:#17171c; --line:#2a2a30;
  --fg:#ececec; --dim:#8a8a92; --faint:#4a4a52;
  --ok:#37e07a; --warn:#ffc233; --crit:#ff4d4d; --cold:#5cc8ff; --info:#b8b8ff;
  --soft:#c81e1e; --med:#e6b800; --hard:#e8e8e8; --inter:#2fbf4f; --wet:#2f6fd8;
  --mono:"Cascadia Mono","Consolas","JetBrains Mono",ui-monospace,monospace;
  --sans:system-ui,"Segoe UI",Roboto,sans-serif;
}
* { box-sizing:border-box; }
html,body {
  margin:0; background:var(--bg); color:var(--fg); font-family:var(--sans);
  font-size:16px; line-height:1.45;
}
body { display:grid; grid-template-columns:16rem minmax(0,1fr); min-height:100vh; }
a { color:var(--info); }
.mono,.num,td,th,.stat b,.badge,code,.chip {
  font-family:var(--mono); font-variant-numeric:tabular-nums;
}
nav.agenda {
  position:sticky; top:0; height:100vh; border-right:1px solid var(--line);
  background:var(--panel); padding:1rem; display:flex; flex-direction:column;
  gap:.2rem; overflow:auto;
}
nav .brand {
  font-family:var(--mono); color:var(--dim); letter-spacing:.12em;
  font-size:.8rem; margin-bottom:.6rem;
}
nav .brand b { color:var(--fg); }
nav a {
  color:var(--dim); text-decoration:none; padding:.35rem .6rem;
  border-left:2px solid transparent; font-size:.95rem;
}
nav a:hover { color:var(--fg); }
nav a.on { color:var(--fg); border-left-color:var(--ok); background:var(--panel2); }
nav a .n { font-family:var(--mono); color:var(--faint); margin-right:.5rem; }
nav .sp { flex:1; }
nav .foot {
  font-size:.78rem; color:var(--faint); font-family:var(--mono);
  line-height:1.5; overflow-wrap:anywhere;
}
main { padding:1.5rem 2.5rem 4rem; max-width:80rem; width:100%; }
section { padding:1.6rem 0; border-bottom:1px solid var(--line); }
section h2 {
  margin:0 0 .9rem; font-family:var(--mono); font-weight:normal;
  letter-spacing:.12em; font-size:.95rem; color:var(--dim);
}
section h2 .n { color:var(--faint); margin-right:.6rem; }
p.lede { margin:0 0 1rem; font-size:1.05rem; max-width:52rem; }
.row { display:grid; gap:1rem; }
.cols-2 { grid-template-columns:1fr 1fr; }
.cols-3 { grid-template-columns:repeat(3,1fr); }
.cols-4 { grid-template-columns:repeat(4,1fr); }
.row.stint-row { align-items:start; }
.card {
  background:var(--panel); border:1px solid var(--line); border-radius:.35rem;
  padding:.9rem 1rem; min-width:0;
}
.card h3 {
  margin:0 0 .5rem; font-family:var(--mono); font-weight:normal;
  font-size:.8rem; letter-spacing:.1em; color:var(--dim);
}
.card .src {
  color:var(--faint); font-size:.72rem; font-family:var(--mono);
  margin:.6rem 0 0;
}
header.session {
  display:flex; align-items:baseline; gap:1.2rem; flex-wrap:wrap;
  padding-bottom:1.2rem; border-bottom:1px solid var(--line);
}
header.session h1 {
  margin:0; font-family:var(--mono); font-size:1.6rem;
  font-weight:bold; letter-spacing:.02em;
}
header.session .meta { color:var(--dim); font-family:var(--mono); font-size:.9rem; }
header.session .meta b { color:var(--fg); font-weight:normal; }
.stat b { display:block; font-size:1.7rem; font-weight:bold; line-height:1.1; }
.stat small { color:var(--dim); }
.ok { color:var(--ok); } .warn { color:var(--warn); }
.crit { color:var(--crit); } .cold { color:var(--cold); }
.info { color:var(--info); } .dim { color:var(--dim); }
.chip {
  display:inline-block; font-size:.72rem; padding:.05rem .45rem;
  border-radius:.25rem; border:1px solid var(--line); color:var(--dim);
  letter-spacing:.06em; white-space:nowrap;
}
.chip.soft { border-color:var(--soft); color:var(--soft); }
.chip.med { border-color:var(--med); color:var(--med); }
.chip.hard { border-color:var(--hard); color:var(--hard); }
.chip.inter { border-color:var(--inter); color:var(--inter); }
.chip.wet { border-color:var(--wet); color:var(--wet); }
.chip.p1 { border-color:var(--crit); color:var(--crit); }
.chip.p2 { border-color:var(--warn); color:var(--warn); }
.chip.p3 { border-color:var(--info); color:var(--info); }
.chip.ok { border-color:var(--ok); color:var(--ok); }
.chip.warn { border-color:var(--warn); color:var(--warn); }
.chip.sup { border-style:dashed; }
.badge { font-size:.7rem; letter-spacing:.12em; padding:.1rem .5rem; border-radius:.25rem; }
.badge.det { background:#173a25; color:var(--ok); }
svg { display:block; width:100%; height:auto; }
.axis { fill:none; stroke:var(--faint); stroke-width:1; }
.grid { stroke:var(--line); stroke-width:1; }
.lbl { fill:var(--dim); font-family:var(--mono); font-size:11px; }
.lbl.b { fill:var(--fg); }
.pt.soft { fill:var(--soft); } .pt.med { fill:var(--med); } .pt.hard { fill:var(--hard); }
.pt.inter { fill:var(--inter); } .pt.wet { fill:var(--wet); }
.pt.unk { fill:var(--dim); }
.pt.inv { fill:none; stroke:var(--faint); stroke-width:1.2; }
.fit { fill:none; stroke-width:1.6; stroke-dasharray:4 3; }
.fit.soft { stroke:var(--soft); } .fit.med { stroke:var(--med); } .fit.hard { stroke:var(--hard); }
.fit.inter { stroke:var(--inter); } .fit.wet { stroke:var(--wet); }
.fit.unk { stroke:var(--dim); }
.band { fill:var(--warn); opacity:.12; }
.pitline { stroke:var(--dim); stroke-width:1; stroke-dasharray:2 3; }
.calltick { fill:var(--warn); }
.calltick.p1 { fill:var(--crit); }
.calltick.sup { fill:var(--faint); }
.stint rect.soft { fill:var(--soft); }
.stint rect.med { fill:var(--med); }
.stint rect.hard { fill:var(--hard); }
.stint rect.inter { fill:var(--inter); } .stint rect.wet { fill:var(--wet); }
.stint rect.unk { fill:var(--dim); }
.stint rect.alt { opacity:.35; }
.stint .lbl { fill:var(--bg); }
.trace { fill:none; stroke-width:1.5; }
.trace.me { stroke:var(--ok); stroke-width:2.2; }
.scroll { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:.86rem; }
th {
  text-align:left; color:var(--dim); font-weight:normal; letter-spacing:.06em;
  font-size:.74rem; padding:.35rem .5rem; border-bottom:1px solid var(--line);
}
td { padding:.4rem .5rem; border-bottom:1px solid #1c1c22; vertical-align:top; }
td.r,th.r { text-align:right; }
tr.sup td { color:var(--faint); }
article { border-left:3px solid var(--ok); padding:.1rem 1rem; margin:.8rem 0; }
.grade { display:inline-flex; gap:.25rem; white-space:nowrap; }
.grade button {
  font-family:var(--mono); font-size:.7rem; border:1px solid var(--line);
  color:var(--dim); background:transparent; padding:.05rem .4rem;
  border-radius:.2rem; cursor:pointer;
}
.grade button.on { border-color:var(--ok); color:var(--ok); }
.grade button.on.bad { border-color:var(--crit); color:var(--crit); }
details { margin:.6rem 0; }
details.why { margin-top:.25rem; font-size:.8rem; color:var(--dim); }
details.why summary { cursor:pointer; color:var(--faint); }
details.why code { color:var(--dim); }
pre { white-space:pre-wrap; overflow-wrap:anywhere; }
ol.actions { margin:0; padding-left:1.4rem; }
ol.actions li { margin:.4rem 0; }
body.index { display:block; min-height:100vh; }
body.index main { margin:0 auto; max-width:80rem; }
body.index .brand { font-family:var(--mono); color:var(--dim); letter-spacing:.12em; }
@media (max-width:900px) {
  body { grid-template-columns:1fr; }
  nav.agenda { position:static; height:auto; }
.cols-2 { grid-template-columns:1fr 1fr; }
  .cols-3,.cols-4 { grid-template-columns:1fr; }
  main { padding:1.5rem 1rem 3rem; }
}
@media print {
  nav,.grade,button { display:none; }
  body { display:block; background:#fff; color:#000; }
  main { max-width:none; padding:0; }
  .card { break-inside:avoid; border-color:#999; }
  details { display:block; }
  .lbl { fill:#333; }
}
</style>"""

_NAV_SCRIPT = """<script>
const links = [...document.querySelectorAll('nav.agenda a')];
const io = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    links.forEach(link => link.classList.toggle(
      'on', link.getAttribute('href') === '#' + entry.target.id
    ));
  });
}, { rootMargin: '-40% 0px -55% 0px' });
document.querySelectorAll('main section').forEach(section => io.observe(section));
</script>"""

_VISUAL_COMPOUNDS = {
    16: ("soft", "SOFT"),
    17: ("med", "MEDIUM"),
    18: ("hard", "HARD"),
    7: ("inter", "INTER"),
    8: ("wet", "WET"),
}
_ACTUAL_COMPOUNDS = {
    22: "C6",
    16: "C5",
    17: "C4",
    18: "C3",
    19: "C2",
    20: "C1",
    21: "C0",
}
_SC_WORDS = {0: "green", 1: "SC", 2: "VSC", 3: "formation"}
_TRACK_NAMES = {
    0: "Melbourne",
    2: "Shanghai",
    3: "Sakhir",
    4: "Catalunya",
    5: "Monaco",
    6: "Montreal",
    7: "Silverstone",
    9: "Hungaroring",
    10: "Spa",
    11: "Monza",
    12: "Singapore",
    13: "Suzuka",
    14: "Abu Dhabi",
    15: "Texas",
    16: "Brazil",
    17: "Austria",
    19: "Mexico",
    20: "Baku",
    26: "Zandvoort",
    27: "Imola",
    29: "Jeddah",
    30: "Miami",
    31: "Las Vegas",
    32: "Losail",
    39: "Silverstone reverse",
    40: "Austria reverse",
    41: "Zandvoort reverse",
}
_SESSION_LABELS = {
    1: "P1",
    2: "P2",
    3: "P3",
    4: "Short practice",
    5: "Q1",
    6: "Q2",
    7: "Q3",
    8: "Short qualifying",
    9: "One-shot qualifying",
    15: "Race",
    16: "Race 2",
    17: "Race 3",
    18: "Time trial",
}
_SECTIONS = (
    ("summary", "00", "Summary"),
    ("pace", "01", "Pace and stints"),
    ("sectors", "02", "Sectors"),
    ("tyres", "03", "Tyres"),
    ("strategy", "04", "Strategy calls"),
    ("radio", "05", "Radio and decisions"),
    ("incidents", "06", "Incidents and energy"),
    ("actions", "07", "Actions"),
)


class _Markup(str):
    pass


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def rules_version(settings: Settings) -> str:
    payload = [rule.model_dump(mode="json") for rule in settings.rules]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:8]


def _compound(visual: int, compound: int) -> tuple[str, str]:
    if visual in _VISUAL_COMPOUNDS:
        return _VISUAL_COMPOUNDS[visual]
    if compound in (7, 8):
        return _VISUAL_COMPOUNDS[compound]
    if compound in _ACTUAL_COMPOUNDS:
        return "unk", _ACTUAL_COMPOUNDS[compound]
    return "unk", "UNKNOWN"


def _compound_for_stint(stint: StintRow, laps: list[LapRow]) -> tuple[str, str]:
    visual = next(
        (
            lap.visual
            for lap in laps
            if stint.start_lap <= lap.lap_num <= stint.end_lap and lap.visual
        ),
        0,
    )
    return _compound(visual, stint.compound)


def _chip(css_class: str, label: object) -> _Markup:
    return _Markup(f"<span class='chip {_esc(css_class)}'>{_esc(label)}</span>")


def _cell(value: object) -> str:
    if isinstance(value, _Markup):
        return f"<td>{value}</td>"
    if isinstance(value, float):
        value = f"{value:.1f}"
    return f"<td>{_esc(value)}</td>"


def _table(headers: tuple[str, ...], rows: Sequence[tuple[object, ...]], source: str) -> str:
    head = "".join(f"<th scope='col'>{_esc(label)}</th>" for label in headers)
    body = "".join("<tr>" + "".join(_cell(value) for value in row) + "</tr>" for row in rows)
    return (
        f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody>"
        f"</table></div><p class='src'>Source: {_esc(source)}.</p>"
    )


def _card(title: str, content: str, source: str | None = None) -> str:
    source_line = f"<p class='src'>Source: {_esc(source)}.</p>" if source else ""
    return f"<div class='card'><h3>{_esc(title)}</h3>{content}{source_line}</div>"


def _section(section_id: str, num: str, title: str, content: str) -> str:
    return (
        f"<section id='{_esc(section_id)}'><h2><span class='n'>{_esc(num)}</span>"
        f"{_esc(title)} <span class='badge det'>DETERMINISTIC</span></h2>{content}</section>"
    )


def _as_int(value: object) -> int | None:
    if not isinstance(value, (int, str, float, bytes, bytearray)):
        return None
    try:
        return int(value)
    except (ValueError, OverflowError):
        return None


def _track_name(track_id: object) -> str:
    if track_id is None:
        return "Track —"
    key = _as_int(track_id)
    if key is None:
        return f"Track {track_id}"
    return _TRACK_NAMES.get(key, f"Track {key}")


def _session_label(session_type: object) -> str:
    if session_type is None:
        return "Session —"
    key = _as_int(session_type)
    if key is None:
        return f"Session {session_type}"
    return _SESSION_LABELS.get(key, f"Session {key}")


def _lap_time(lap_time_ms: int) -> str:
    tenths = round(lap_time_ms / 100)
    minutes, remainder = divmod(tenths, 600)
    return f"{minutes}:{remainder // 10:02d}.{remainder % 10}"


def _pit_laps(
    pits: list[PitEventRow],
    laps: list[LapRow],
    session_type: object,
) -> list[int]:
    found = {pit.lap_num for pit in pits}
    value = _as_int(session_type)
    if value is not None and session_kind(value) == "race":
        found.update(stop_laps(laps))
    return sorted(found)


def _sc_runs(laps: list[LapRow]) -> list[tuple[int, int, str]]:
    safety_car_laps = [lap for lap in laps if lap.sc_status in (1, 2)]
    if not safety_car_laps:
        return []
    runs: list[tuple[int, int, int]] = []
    start = previous = safety_car_laps[0].lap_num
    status = safety_car_laps[0].sc_status
    for lap in safety_car_laps[1:]:
        if lap.lap_num != previous + 1 or lap.sc_status != status:
            runs.append((start, previous, status))
            start = lap.lap_num
            status = lap.sc_status
        previous = lap.lap_num
    runs.append((start, previous, status))
    return [(start_lap, end_lap, _SC_WORDS[status]) for start_lap, end_lap, status in runs]


def _fit_points(stint: StintRow, laps: list[LapRow]) -> list[tuple[int, float]]:
    usable = [
        lap
        for lap in laps
        if stint.start_lap <= lap.lap_num <= stint.end_lap
        and lap.valid == 1
        and lap.sc_status == 0
        and lap.lap_time_ms > 0
    ]
    burned = fuel_burned_laps(usable)
    return [
        (
            lap.lap_num,
            stint.base_ms + stint.deg_ms_per_lap * lap.tyre_age_laps - stint.fuel_ms_per_lap * fuel,
        )
        for lap, fuel in zip(usable, burned, strict=True)
    ]


def _chart_x(lap_num: float, first: int, last: int, left: float, right: float) -> float:
    return left + (lap_num - first) / max(1, last - first) * (right - left)


def _lap_chart(laps: list[LapRow], stints: list[StintRow], pit_laps: list[int]) -> str:
    timed = [lap for lap in laps if lap.lap_time_ms > 0]
    if not timed:
        return "<p>No lap times recorded.</p>"

    clean = [lap for lap in timed if lap.valid and lap.sc_status == 0]
    range_laps = clean or timed
    ymin = min(lap.lap_time_ms for lap in range_laps) - 500
    ymax = max(lap.lap_time_ms for lap in range_laps) + 1000
    span = ymax - ymin
    step = 500 if span <= 4000 else 1000 if span <= 10000 else 2000
    first = min(lap.lap_num for lap in timed)
    last = max(lap.lap_num for lap in timed)
    height = 300
    left, right, top, bottom = 60, 940, 14, 34

    def x(lap_num: float) -> float:
        return _chart_x(lap_num, first, last, left, right)

    def y(value: float) -> float:
        return height - bottom - (value - ymin) / span * (height - top - bottom)

    parts = [
        "<svg id='lapchart' viewBox='0 0 960 300' role='img' "
        "aria-label='Lap times by lap, coloured by compound'>"
    ]
    for start_lap, end_lap, label in _sc_runs(laps):
        x1, x2 = x(start_lap - 0.5), x(end_lap + 0.5)
        parts.append(
            f"<rect class='band' x='{x1:.1f}' y='{top}' width='{x2 - x1:.1f}' "
            f"height='{height - top - bottom}'/>"
        )
        parts.append(
            f"<text class='lbl' x='{(x1 + x2) / 2:.1f}' y='{top + 12}' "
            f"text-anchor='middle'>{_esc(label)}</text>"
        )
    parts.append(f"<path class='axis' d='M{left} {top}V{height - bottom}H{right}'/>")
    grid = ((ymin + step - 1) // step) * step
    while grid <= ymax:
        yy = y(grid)
        parts.append(f"<line class='grid' x1='{left}' x2='{right}' y1='{yy:.1f}' y2='{yy:.1f}'/>")
        parts.append(
            f"<text class='lbl' x='{left - 8}' y='{yy + 4:.1f}' text-anchor='end'>"
            f"{_esc(_lap_time(grid))}</text>"
        )
        grid += step
    lap_step = max(1, round(max(1, last - first) / 8))
    tick = first
    while tick <= last:
        parts.append(
            f"<text class='lbl' x='{x(tick):.1f}' y='{height - 12}' "
            f"text-anchor='middle'>{_esc(f'L{tick}')}</text>"
        )
        tick += lap_step
    if last != first and (last - first) % lap_step:
        parts.append(
            f"<text class='lbl' x='{x(last):.1f}' y='{height - 12}' "
            f"text-anchor='middle'>{_esc(f'L{last}')}</text>"
        )
    for pit_lap in pit_laps:
        pit_x = x(pit_lap + 0.5)
        parts.append(
            f"<line class='pitline' x1='{pit_x:.1f}' x2='{pit_x:.1f}' "
            f"y1='{top}' y2='{height - bottom}'/>"
        )
        parts.append(
            f"<text class='lbl' x='{pit_x + 4:.1f}' y='{height - bottom - 6}'>"
            f"{_esc(f'PIT L{pit_lap}')}</text>"
        )
    for stint in stints:
        fit_points = _fit_points(stint, laps)
        if len(fit_points) < 2:
            continue
        css_class, word = _compound_for_stint(stint, laps)
        points = " ".join(
            f"{x(lap_num):.1f},{y(fitted_ms):.1f}" for lap_num, fitted_ms in fit_points
        )
        title = f"{word} fitted degradation {stint.deg_ms_per_lap:.1f} ms/lap"
        parts.append(
            f"<polyline class='fit {_esc(css_class)}' points='{_esc(points)}'>"
            f"<title>{_esc(title)}</title></polyline>"
        )
        midpoint = len(fit_points) // 2
        if len(fit_points) % 2:
            mid_lap: float = float(fit_points[midpoint][0])
            mid_ms: float = fit_points[midpoint][1]
        else:
            before, after = fit_points[midpoint - 1 : midpoint + 1]
            mid_lap = (before[0] + after[0]) / 2
            mid_ms = (before[1] + after[1]) / 2
        sign = "+" if stint.deg_ms_per_lap >= 0 else ""
        parts.append(
            f"<text class='lbl' x='{x(mid_lap):.1f}' y='{y(mid_ms) - 8:.1f}' "
            f"text-anchor='middle'>{_esc(f'{word} {sign}{stint.deg_ms_per_lap:.0f} ms/lap')}</text>"
        )
    for lap in timed:
        css_class, word = _compound(lap.visual, lap.compound)
        slow = lap.lap_time_ms > ymax
        invalid = not lap.valid or lap.sc_status > 0 or slow
        cy = top if slow else min(height - bottom, max(top, y(lap.lap_time_ms)))
        reason = ", ".join(lap.invalid_reasons) or (
            _SC_WORDS.get(lap.sc_status, str(lap.sc_status))
            if lap.sc_status
            else "invalid"
            if not lap.valid
            else "valid"
        )
        title = (
            f"Lap {lap.lap_num}: {_lap_time(lap.lap_time_ms)}, {word}, "
            f"age {lap.tyre_age_laps}, {reason}"
        )
        parts.append(
            f"<circle class='pt {'inv' if invalid else _esc(css_class)}' "
            f"cx='{x(lap.lap_num):.1f}' cy='{cy:.1f}' r='{4 if invalid else 4.5}'>"
            f"<title>{_esc(title)}</title></circle>"
        )
        if slow:
            parts.append(
                f"<text class='lbl' x='{x(lap.lap_num):.1f}' y='{top + 30}' "
                f"text-anchor='middle'>{_esc('▲')}</text>"
            )
    parts.append("</svg>")
    return "".join(parts)


def _stint_bars(laps: list[LapRow], stints: list[StintRow]) -> str:
    first = min((lap.lap_num for lap in laps), default=1)
    last = max((lap.lap_num for lap in laps), default=1)
    left, right = 58, 450

    def x(lap_num: float) -> float:
        return left + (lap_num - first + 0.5) / max(1, last - first + 1) * (right - left)

    parts = ["<svg id='stintbars' viewBox='0 0 460 70' role='img' aria-label='Tyre stints'>"]
    parts.append("<text class='lbl b' x='50' y='27' text-anchor='end'>YOU</text>")
    for stint in stints:
        css_class, word = _compound_for_stint(stint, laps)
        start_x = x(stint.start_lap)
        end_x = x(stint.end_lap + 1)
        parts.append(
            f"<g class='stint'><rect class='{_esc(css_class)}' x='{start_x:.1f}' "
            f"y='12' width='{max(2, end_x - start_x - 2):.1f}' height='24' rx='2'/>"
            f"<text x='{start_x + 5:.1f}' y='28' class='lbl'>"
            f"{_esc(f'{word} {stint.start_lap}–{stint.end_lap}')}</text></g>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _race_trace(laps: list[LapRow], stints: list[StintRow], pit_laps: list[int]) -> str:
    timed = [lap for lap in laps if lap.lap_time_ms > 0]
    svg = ["<svg id='racetrace' viewBox='0 0 460 200' role='img' aria-label='Race trace'>"]
    if not timed:
        return "".join(svg) + "</svg>"
    clean = [lap.lap_time_ms for lap in timed if lap.valid and lap.sc_status == 0]
    reference = statistics.median(clean or [lap.lap_time_ms for lap in timed])
    cumulative = 0.0
    trace: list[tuple[int, float]] = []
    for lap in timed:
        cumulative += (lap.lap_time_ms - reference) / 1000
        trace.append((lap.lap_num, cumulative))
    low = min(0.0, *(value for _, value in trace))
    high = max(0.0, *(value for _, value in trace))
    padding = max(1.0, (high - low) * 0.1)
    low -= padding
    high += padding
    span = high - low
    first, last = timed[0].lap_num, timed[-1].lap_num
    left, right, top, bottom = 44, 450, 10, 174

    def x(lap_num: float) -> float:
        return _chart_x(lap_num, first, last, left, right)

    def y(value: float) -> float:
        return top + (high - value) / span * (bottom - top)

    tick_step = 5 if span <= 30 else 10 if span <= 60 else 20
    tick = int(low // tick_step) * tick_step
    while tick <= high:
        yy = y(tick)
        svg.append(f"<line class='grid' x1='{left}' x2='{right}' y1='{yy:.1f}' y2='{yy:.1f}'/>")
        label = f"+{tick}" if tick > 0 else str(tick)
        svg.append(
            f"<text class='lbl' x='{left - 6}' y='{yy + 4:.1f}' "
            f"text-anchor='end'>{_esc(label)}</text>"
        )
        tick += tick_step
    for start_lap, end_lap, _ in _sc_runs(laps):
        x1, x2 = x(start_lap - 0.5), x(end_lap + 0.5)
        svg.append(
            f"<rect class='band' x='{x1:.1f}' y='{top}' width='{x2 - x1:.1f}' "
            f"height='{bottom - top}'/>"
        )
    svg.append(f"<line class='axis' x1='{left}' x2='{right}' y1='{y(0):.1f}' y2='{y(0):.1f}'/>")
    for pit_lap in pit_laps:
        pit_x = x(pit_lap + 0.5)
        svg.append(
            f"<line class='pitline' x1='{pit_x:.1f}' x2='{pit_x:.1f}' y1='{top}' y2='{bottom}'/>"
        )
    path = " ".join(
        f"{'M' if index == 0 else 'L'}{x(lap_num):.1f} {y(value):.1f}"
        for index, (lap_num, value) in enumerate(trace)
    )
    svg.append(f"<path class='trace me' d='{path}'/>")
    lap_step = max(1, round(max(1, last - first) / 4))
    for lap_num in range(first, last + 1, lap_step):
        svg.append(
            f"<text class='lbl' x='{x(lap_num):.1f}' y='194' "
            f"text-anchor='middle'>{_esc(f'L{lap_num}')}</text>"
        )
    svg.append("</svg>")
    return "".join(svg)


def _priority_chip(priority: object) -> _Markup | str:
    key = _as_int(priority)
    if key is None:
        return _esc(priority if priority is not None else "—")
    if key not in (1, 2, 3):
        return _esc(key)
    return _chip(f"p{key}", f"P{key}")


def _verdict(value: str) -> _Markup | str:
    classes = {"good": "ok", "noise": "warn", "too_late": "warn", "wrong": "p1"}
    if value not in classes:
        return _esc(value)
    label = "LATE" if value == "too_late" else value.upper()
    return _chip(classes[value], label)


def _grade_buttons(call_id: str, grade: str | None) -> str:
    buttons = []
    for value, label in (
        ("good", "good"),
        ("noise", "noise"),
        ("too_late", "late"),
        ("wrong", "wrong"),
    ):
        active_class = (
            " class='on" + (" bad" if value != "good" else "") + "'" if grade == value else ""
        )
        buttons.append(
            f"<button type='button'{active_class} data-call='{_esc(call_id)}' "
            f"data-grade='{_esc(value)}'>{_esc(label)}</button>"
        )
    return "<div class='grade'>" + "".join(buttons) + "</div>"


def _provenance(session: dict[str, Any], calls: list[dict[str, Any]], settings: Settings) -> str:
    recording_path = str(session.get("recording_path") or "")
    basename = os.path.basename(recording_path) if recording_path else ""
    recording = f"recording {basename}" if basename else "recording not linked"
    if recording_path:
        try:
            size_mb = os.path.getsize(recording_path) / (1024 * 1024)
        except OSError:
            pass
        else:
            recording += f" · {size_mb:.1f} MB"
    current = config_hash(settings)
    resolved_hash = session.get("config_hash") or ""
    if not resolved_hash:
        call_configs = Counter(
            str(call["config_hash"]) for call in calls if call.get("config_hash")
        )
        resolved_hash = call_configs.most_common(1)[0][0] if call_configs else "unknown"
    matches = resolved_hash == current
    mindsets = Counter(str(call["mindset"]) for call in calls if call.get("mindset"))
    mindset = mindsets.most_common(1)[0][0] if calls and mindsets else "unknown"
    is_synthetic = bool(session.get("synthetic")) or is_synthetic_uid(int(session.get("uid") or 0))
    origin = ["synthetic"] if is_synthetic else []
    if is_synthetic and session.get("derived_from"):
        origin.append(f"derived from session UID {session['derived_from']}")
    lines = (
        recording,
        *origin,
        f"profile {settings.recording.profile if matches else 'unknown'}",
        f"config {resolved_hash}",
        f"rules {rules_version(settings) if matches else 'unknown'}",
        f"mindset {mindset}",
    )
    content = "<br>".join(_esc(line) for line in lines)
    return f"<div class='foot' id='provenance'>{content}</div>"


def _stint_summary(stints: list[StintRow], laps: list[LapRow]) -> str:
    return " → ".join(
        f"{_compound_for_stint(stint, laps)[1]} {stint.start_lap}–{stint.end_lap}"
        for stint in stints
    )


def _summary_section(
    session: dict[str, Any],
    laps: list[LapRow],
    pits: list[PitEventRow],
    pit_laps: list[int],
    calls: list[dict[str, Any]],
    grades: dict[str, dict[str, Any]],
) -> str:
    clean = [lap for lap in laps if lap.valid and lap.sc_status == 0 and lap.lap_time_ms > 0]
    fired = [call for call in calls if call.get("outcome") == "fired"]
    judged = Counter(
        str(grades.get(str(call.get("call_id")), {}).get("grade") or "ungraded") for call in fired
    )
    mean = _lap_time(round(statistics.fmean(lap.lap_time_ms for lap in clean))) if clean else "—"
    spread = f"{statistics.pstdev(lap.lap_time_ms for lap in clean) / 1000:.1f} s" if clean else "—"
    stop_count = len(pit_laps)
    pit = next((event for event in pits if event.lap_num), None)
    if pit is not None:
        pit_value = f"L{pit.lap_num} · {pit.loss_ms / 1000:.1f} s"
        pit_note = "measured pit loss"
    elif pit_laps:
        pit_value = f"L{pit_laps[0]}"
        pit_note = "loss not measured"
    else:
        pit_value = "none"
        pit_note = "no pit stop recorded"
    summary_text = (
        f"{len(laps)} laps · {len(clean)} clean green laps · {stop_count} "
        f"{'stop' if stop_count == 1 else 'stops'} · "
        f"{len(fired)} calls · {judged['good']} graded good · {judged['wrong']} graded wrong. "
        f"Mean clean pace {mean}, spread {spread}."
    )
    stats = (
        ("RACE PACE", mean, f"mean clean lap · {len(clean)} laps", ""),
        ("CONSISTENCY", spread, "σ across clean laps", ""),
        ("PIT STOP", pit_value, pit_note, "warn" if pit_value != "none" else ""),
        (
            "CALLS",
            f"{len(fired)} / {judged['good']} good",
            f"{judged['noise']} noise · {judged['too_late']} late · {judged['wrong']} wrong",
            "",
        ),
    )
    cards = []
    for title, value, note, css_class in stats:
        value_class = f" class='{css_class}'" if css_class else ""
        cards.append(
            f"<div class='card stat'><h3>{_esc(title)}</h3><b{value_class}>"
            f"{_esc(value)}</b><small>{_esc(note)}</small></div>"
        )
    table = _table(
        ("Track", "Track name", "Session", "Calls mode", "Recording"),
        [
            (
                session.get("track_id") if session.get("track_id") is not None else "—",
                _track_name(session.get("track_id")),
                session.get("session_type") if session.get("session_type") is not None else "—",
                session.get("calls_mode") or "unknown",
                session.get("recording_path") or "not linked",
            )
        ],
        "sessions",
    )
    content = (
        f"<p class='lede'>{_esc(summary_text)}</p><div class='row cols-4'>"
        + "".join(cards)
        + "</div>"
        + _card("SESSION DETAILS", table)
    )
    return _section("summary", "00", "Summary", content)


def _pace_section(laps: list[LapRow], stints: list[StintRow], pit_laps: list[int]) -> str:
    chart = _card(
        "LAP TIME BY LAP · COLOUR = COMPOUND · HOLLOW = INVALID · DASHED = DEG FIT · AMBER = SC",
        _lap_chart(laps, stints, pit_laps),
        "laps (validity and invalid_reasons) · stints (degradation fit) · pit_events",
    )
    stint_chart = _card(
        "STINTS · YOUR COMPOUNDS",
        _stint_bars(laps, stints),
        "stints · laps (visual compound)",
    )
    trace_chart = _card(
        "RACE TRACE · TIME VS YOUR MEDIAN CLEAN LAP, s · JUMPS ARE STOPS",
        _race_trace(laps, stints, pit_laps),
        "laps · median clean lap is the reference",
    )
    lap_rows = [
        (
            lap.lap_num,
            _lap_time(lap.lap_time_ms) if lap.lap_time_ms > 0 else "—",
            _chip(*_compound(lap.visual, lap.compound)),
            lap.tyre_age_laps,
            f"{lap.fuel_kg:.1f}",
            "valid"
            if lap.valid and lap.sc_status == 0
            else ", ".join(lap.invalid_reasons)
            or (_SC_WORDS.get(lap.sc_status, str(lap.sc_status)) if lap.sc_status else "invalid"),
        )
        for lap in laps
    ]
    lap_table = _card(
        "LAP DETAILS",
        _table(
            ("Lap", "Time", "Compound", "Age", "Fuel (kg)", "Validity"),
            lap_rows,
            "laps",
        ),
    )
    stint_rows = [
        (
            _chip(*_compound_for_stint(stint, laps)),
            f"{stint.start_lap}–{stint.end_lap}",
            f"{stint.deg_ms_per_lap:.1f}",
            stint.n_valid_laps,
            f"{stint.rmse_ms:.1f}",
        )
        for stint in stints
    ]
    stint_table = _card(
        "STINT FITS",
        _table(
            ("Compound", "Laps", "Fitted deg (ms/lap)", "Fit laps", "RMSE (ms)"),
            stint_rows,
            "stints; fit values require sufficient valid laps",
        ),
    )
    content = (
        chart
        + "<div class='row cols-2 stint-row'>"
        + stint_chart
        + trace_chart
        + "</div><div class='row cols-2'>"
        + lap_table
        + stint_table
        + "</div>"
    )
    return _section("pace", "01", "Pace and stints", content)


def _sector_section(laps: list[LapRow]) -> str:
    valid = [lap for lap in laps if lap.valid and lap.sc_status == 0 and lap.lap_time_ms > 0]
    rows = [
        (
            label,
            f"{statistics.fmean(values) / 1000:.3f}" if values else "—",
            len(values),
        )
        for label, values in (
            ("S1", [lap.s1_ms for lap in valid if lap.s1_ms > 0]),
            ("S2", [lap.s2_ms for lap in valid if lap.s2_ms > 0]),
            (
                "S3",
                [
                    lap.lap_time_ms - lap.s1_ms - lap.s2_ms
                    for lap in valid
                    if lap.s1_ms > 0 and lap.s2_ms > 0
                ],
            ),
        )
    ]
    card = _card(
        "MEAN SECTOR TIMES",
        _table(("Sector", "Mean (s)", "Clean laps"), rows, "laps.s1_ms, s2_ms, lap_time_ms"),
    )
    return _section("sectors", "02", "Sectors", card)


def _tyres_section(laps: list[LapRow], settings: Settings) -> str:
    valid = [lap for lap in laps if lap.valid and lap.sc_status == 0 and lap.lap_time_ms > 0]
    by_compound: dict[int, list[LapRow]] = {}
    for lap in valid:
        if lap.tyre_inner_c:
            by_compound.setdefault(lap.compound, []).append(lap)
    rows = []
    for compound, group in sorted(by_compound.items()):
        cold, hot = thermal_window(settings.thresholds, compound)
        green = sum(cold <= lap.tyre_inner_c <= hot for lap in group)
        visual = next((lap.visual for lap in group if lap.visual), 0)
        rows.append(
            (
                _chip(*_compound(visual, compound)),
                f"{green / len(group):.0%}",
                len(group),
                cold,
                hot,
            )
        )
    card = _card(
        "TYRE TEMPERATURES",
        _table(
            ("Compound", "In window", "Measured laps", "Cold below (°C)", "Hot above (°C)"),
            rows,
            "laps.tyre_inner_c and configured thresholds; lap means",
        ),
    )
    return _section("tyres", "03", "Tyres", card)


def _hindsight(automatic: list[dict[str, Any]] | None) -> str:
    if not automatic:
        return "—"
    parts = []
    for item in automatic:
        metric = str(item.get("metric") or "").strip()
        label = str(item.get("label") or "").strip()
        if metric and label:
            parts.append(f"{metric}: {label}")
        elif metric or label:
            parts.append(metric or label)
    return " · ".join(parts) or "—"


def _strategy_section(
    calls: list[dict[str, Any]],
    pits: list[PitEventRow],
    pit_laps: list[int],
    grades: dict[str, dict[str, Any]],
    outcomes: dict[str, list[dict[str, Any]]],
) -> str:
    strategy_rows = []
    for call in calls:
        call_id = str(call.get("call_id") or "")
        human = grades.get(call_id)
        automatic = outcomes.get(call_id, [])
        verdict = (
            str(human.get("grade"))
            if human
            else (
                ", ".join(sorted({str(item.get("label") or "") for item in automatic}))
                or "ungraded"
            )
        )
        rule_id = str(call.get("rule_id") or "")
        if call.get("outcome") == "fired" and any(
            word in rule_id for word in ("pit", "box", "stop", "undercut", "extend", "plan")
        ):
            strategy_rows.append(
                (
                    call.get("lap") if call.get("lap") is not None else "—",
                    rule_id,
                    call.get("text") or "",
                    _verdict(verdict),
                    _hindsight(automatic),
                )
            )
    strategy = _card(
        "STRATEGY CALLS",
        _table(
            ("Lap", "Rule", "Called", "Verdict", "Hindsight"),
            strategy_rows,
            "calls, call_grades, outcomes",
        ),
    )
    if pits:
        pit_content = _table(
            ("Pit lap", "Loss (s)", "Neutralised"),
            [
                (event.lap_num, f"{event.loss_ms / 1000:.1f}", bool(event.neutralised))
                for event in pits
            ],
            "pit_events",
        )
    else:
        if pit_laps:
            labels = ", ".join(f"L{lap}" for lap in pit_laps)
            change = "change" if len(pit_laps) == 1 else "changes"
            empty_message = f"No pit events stored. Stint {change} on {labels}."
        else:
            empty_message = "No pit stops."
        pit_content = f"<p>{_esc(empty_message)}</p>"
    pit = _card("PIT STOPS", pit_content, "pit_events")
    return _section("strategy", "04", "Strategy calls", strategy + pit)


def _call_timeline(
    calls: list[dict[str, Any]],
    laps: list[LapRow],
    pit_laps: list[int],
) -> str:
    numbers = [lap.lap_num for lap in laps] + [
        int(call["lap"]) for call in calls if call.get("lap")
    ]
    if not numbers:
        return (
            "<svg id='calltimeline' viewBox='0 0 960 70' role='img' "
            "aria-label='Call timeline'></svg>"
        )
    first, last = min(numbers), max(numbers)
    left, right, baseline = 20, 940, 42

    def x(lap_num: float) -> float:
        return _chart_x(lap_num, first, last, left, right)

    lap_width = (right - left) / max(1, last - first)
    parts = [
        "<svg id='calltimeline' viewBox='0 0 960 70' role='img' aria-label='Calls by lap'>",
        f"<line class='axis' x1='{left}' x2='{right}' y1='{baseline}' y2='{baseline}'/>",
    ]
    for start_lap, end_lap, _ in _sc_runs(laps):
        x1, x2 = x(start_lap - 0.5), x(end_lap + 0.5)
        parts.append(f"<rect class='band' x='{x1:.1f}' y='6' width='{x2 - x1:.1f}' height='36'/>")
    for pit_lap in pit_laps:
        pit_x = x(pit_lap + 0.5)
        parts.append(
            f"<line class='pitline' x1='{pit_x:.1f}' x2='{pit_x:.1f}' y1='6' y2='{baseline}'/>"
        )
    by_lap: dict[int, list[dict[str, Any]]] = {}
    for call in calls:
        if call.get("lap") is not None:
            by_lap.setdefault(int(call["lap"]), []).append(call)
    for lap_num, grouped in by_lap.items():
        offset_limit = max(0.0, lap_width / 2 - 2.5)
        for index, call in enumerate(grouped):
            offset = (index - (len(grouped) - 1) / 2) * 6
            offset = min(offset_limit, max(-offset_limit, offset))
            outcome = str(call.get("outcome") or "")
            priority_one = outcome == "fired" and call.get("priority") == 1
            suppressed = outcome == "suppressed"
            height = 8 if suppressed else 26 if priority_one else 18
            css_class = " sup" if suppressed else " p1" if priority_one else ""
            parts.append(
                f"<rect class='calltick{css_class}' x='{x(lap_num) + offset - 2.5:.1f}' "
                f"y='{baseline - height}' width='5' height='{height}'/>"
            )
    tick_step = max(1, round(max(1, last - first) / 8))
    for lap_num in range(first, last + 1, tick_step):
        parts.append(
            f"<text class='lbl' x='{x(lap_num):.1f}' y='64' "
            f"text-anchor='middle'>{_esc(f'L{lap_num}')}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _radio_section(
    calls: list[dict[str, Any]],
    laps: list[LapRow],
    pit_laps: list[int],
    grades: dict[str, dict[str, Any]],
    outcomes: dict[str, list[dict[str, Any]]],
    inputs: list[dict[str, Any]],
    *,
    editable: bool,
) -> str:
    timeline = _card(
        "CALL TIMELINE",
        _call_timeline(calls, laps, pit_laps),
        "calls · safety-car laps · pit events",
    )
    rows = []
    for call in calls:
        call_id = str(call.get("call_id") or "")
        human = grades.get(call_id)
        automatic = outcomes.get(call_id, [])
        verdict = (
            str(human.get("grade"))
            if human
            else (
                ", ".join(sorted({str(item.get("label") or "") for item in automatic}))
                or "ungraded"
            )
        )
        raw_inputs = call.get("inputs")
        try:
            parsed_inputs = json.loads(raw_inputs) if raw_inputs else {}
        except (TypeError, json.JSONDecodeError):
            parsed_inputs = {"raw": raw_inputs}
        detail = json.dumps(parsed_inputs, sort_keys=True, indent=2, default=str)
        evidence = json.dumps(automatic, sort_keys=True, default=str)
        outcome = str(call.get("outcome") or "")
        said = str(call.get("text") or "—")
        decision = (
            "suppressed · " + str(call.get("suppressed_by") or "unknown")
            if outcome == "suppressed"
            else outcome
        )
        details = (
            "<details class='why'><summary>why</summary><pre>"
            + _esc(detail)
            + "</pre><pre>"
            + _esc(evidence)
            + "</pre></details>"
        )
        grade_cell = (
            _grade_buttons(call_id, str(human.get("grade")) if human else None)
            if editable and call_id
            else _verdict(verdict)
        )
        row_class = " class='sup'" if outcome == "suppressed" else ""
        lap_value = call.get("lap") if call.get("lap") is not None else "—"
        rows.append(
            f"<tr{row_class}><td>{_esc(lap_value)}</td>"
            f"<td>{_priority_chip(call.get('priority'))}</td>"
            f"<td class='mono'>{_esc(call.get('rule_id') or '—')}</td>"
            f"<td>{_esc(said)}{details}</td><td>{_esc(decision)}</td><td>{grade_cell}</td></tr>"
        )
    header = "".join(
        f"<th scope='col'>{_esc(value)}</th>"
        for value in ("Lap", "Pri", "Rule", "Said", "Decision", "Grade")
    )
    body = "".join(rows) or "<tr><td colspan='6'>No calls recorded.</td></tr>"
    call_table = (
        "<div class='scroll'><table class='calls'><thead><tr>"
        + header
        + "</tr></thead><tbody>"
        + body
        + "</tbody></table></div><p class='src'>Source: calls · decision log · driver input.</p>"
    )
    calls_card = _card("CALLS AND DECISIONS", call_table)
    input_card = _card(
        "DRIVER INPUTS",
        _table(
            ("Lap", "Input", "Rule"),
            [
                (
                    item.get("lap") if item.get("lap") is not None else "—",
                    item.get("action") or "",
                    item.get("rule_id") or "",
                )
                for item in inputs
            ],
            "driver_inputs",
        ),
    )
    return _section(
        "radio",
        "05",
        "Radio and decisions",
        timeline + calls_card + input_card,
    )


def _incidents_section(laps: list[LapRow]) -> str:
    rows = [
        (
            lap.lap_num,
            f"{lap.ers_deployed_j / 1_000_000:.1f}",
            f"{lap.fuel_kg:.1f}",
            _SC_WORDS.get(lap.sc_status, lap.sc_status),
        )
        for lap in laps
    ]
    card = _card(
        "INCIDENTS AND ENERGY",
        _table(
            ("Lap", "ERS deployed (MJ)", "Fuel (kg)", "SC status"),
            rows,
            "laps; raw event details require the recording index",
        ),
    )
    return _section("incidents", "06", "Incidents and energy", card)


def _setup_number(value: object) -> str:
    if isinstance(value, int | float):
        return f"{value:g}"
    return str(value)


def _setup_actions(db: Database, uid: int, session: dict[str, Any], settings: Settings) -> str:
    stored = [row for row in db.setup_recs_for_session(uid) if row.get("mode") == "debrief"]
    rows: list[tuple[object, ...]] = []
    locked: set[tuple[str, str]] = set()
    event_laps = 0
    source = "setup_recs, laps, setup_states"
    if stored:
        for rec in stored:
            evidence = rec.get("evidence", {})
            signals = evidence.get("signals", {}) if isinstance(evidence, dict) else {}
            if isinstance(signals, dict):
                event_laps = max(event_laps, int(signals.get("event_laps", 0) or 0))
            from_value = float(rec.get("from_value") or 0)
            to_value = evidence.get("to_value", from_value + float(rec.get("delta") or 0))
            rows.append(
                (
                    evidence.get("tier", ""),
                    rec.get("param", ""),
                    f"{_setup_number(from_value)} → {_setup_number(to_value)}",
                    rec.get("conf", ""),
                    evidence.get("expect", ""),
                    evidence.get("tradeoff", ""),
                    ", ".join(
                        f"{key}={value}"
                        for key, value in sorted(signals.items())
                        if key not in {"run_laps", "compound", "setup_state_id"}
                    )
                    if isinstance(signals, dict)
                    else "",
                )
            )
            suppressions = evidence.get("suppressed", []) if isinstance(evidence, dict) else []
            if isinstance(suppressions, list):
                locked.update(
                    (str(item.get("param", "")), str(rec.get("rule_id", "")))
                    for item in suppressions
                    if isinstance(item, dict) and item.get("reason") == "locked"
                )
    else:
        parc_ferme_value = session.get("parc_ferme")
        parc_ferme = int(parc_ferme_value) if parc_ferme_value is not None else -1
        advice = recommend_for_session(
            db,
            uid,
            settings,
            "debrief",
            run_choice="longest",
            parc_ferme=parc_ferme,
            lap=None,
            store=False,
        )
        if advice is not None:
            event_laps = advice.signals.event_laps
            suppressions = explain(
                advice.signals,
                advice.setup,
                mode="debrief",
                parc_ferme=parc_ferme,
                rules=advice.rules,
                thresholds=settings.thresholds,
                learned=advice.learned,
            )
            recommendations = advice.recommendations
            locked.update(
                (item.get("param", ""), item.get("rule_id", ""))
                for item in suppressions
                if item.get("reason") == "locked"
            )
            for recommendation in recommendations:
                event_laps = max(
                    event_laps,
                    int(recommendation.evidence.get("event_laps", 0) or 0),
                )
                rows.append(
                    (
                        recommendation.tier,
                        recommendation.param,
                        f"{_setup_number(recommendation.from_value)} "
                        f"→ {_setup_number(recommendation.to_value)}",
                        recommendation.conf,
                        recommendation.expect,
                        recommendation.tradeoff,
                        ", ".join(
                            f"{key}={value}"
                            for key, value in sorted(recommendation.evidence.items())
                            if key not in {"run_laps", "compound", "setup_state_id"}
                        ),
                    )
                )
                locked.update(
                    (item.get("param", ""), recommendation.rule_id)
                    for item in recommendation.suppressed
                    if item.get("reason") == "locked"
                )

    content = (
        _table(
            (
                "Tier",
                "Parameter",
                "Current → proposed",
                "Confidence",
                "Why",
                "Trade-off",
                "Evidence",
            ),
            rows,
            source,
        )
        if rows
        else (
            "<p>No setup change suggested: no symptom passed its threshold. "
            f"Run event laps: {event_laps}.</p><p class='src'>Source: {_esc(source)}.</p>"
        )
    )
    locked_html = "".join(
        f"<p>Would suggest {_esc(param)} ({_esc(rule)}), locked by parc fermé.</p>"
        for param, rule in sorted(locked)
        if param and rule
    )
    return content + locked_html


def _actions_section(
    db: Database,
    uid: int,
    settings: Settings,
    session: dict[str, Any],
    grades: dict[str, dict[str, Any]],
) -> str:
    learned = learned_state(
        db, settings, int(session["track_id"] if session.get("track_id") is not None else -1)
    )
    counts = Counter(
        str(grade.get("rule_id") or "")
        for grade in grades.values()
        if grade.get("grade") in ("noise", "wrong", "too_late")
    )
    findings = [
        f"<li>Review {_esc(rule)}: {count} negative grades.</li>"
        for rule, count in sorted(counts.items())
        if rule and count > 0
    ]
    actions = (
        "<ol class='actions'>" + "".join(findings) + "</ol>"
        if findings
        else "<p>No actions from grades yet.</p>"
    )
    content = (
        actions
        + "<details><summary>Persisted learning</summary><pre>"
        + _esc(json.dumps(learned, indent=2, default=str))
        + "</pre></details>"
        + "<p class='src'>Source: call_grades, model_params, sessions.</p>"
    )
    return _section(
        "actions",
        "07",
        "Actions",
        _card("SETUP", _setup_actions(db, uid, session, settings))
        + _card("ACTIONS FOR NEXT SESSION", content),
    )


def _grade_script(uid: int) -> str:
    return (
        "<script>document.addEventListener('click',async e=>{"
        "const b=e.target.closest('button[data-grade]');if(!b)return;"
        f"const r=await fetch('/api/debrief/{_esc(uid)}/grade',{{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({call_id:b.dataset.call,grade:b.dataset.grade})});"
        "if(r.ok)location.reload();else alert('Grade was not saved');});</script>"
    )


def render_debrief(db: Database, uid: int, settings: Settings, *, editable: bool = False) -> str:
    session = db.session_row(uid)
    if session is None:
        raise ValueError(f"session {uid} not found")
    laps = db.laps_for(uid)
    stints = db.stints_for_session(uid)
    pits = db.pit_events_for_session(uid)
    pit_laps = _pit_laps(pits, laps, session.get("session_type"))
    calls = db.calls_for_session(uid)
    grades = {str(row["call_id"]): row for row in db.grades_for_session(uid)}
    outcomes: dict[str, list[dict[str, Any]]] = {}
    for row in db.outcomes_for_session(uid):
        outcomes.setdefault(str(row["call_id"]), []).append(row)
    inputs = db.driver_inputs_for_session(uid)
    track = _track_name(session.get("track_id")).upper()
    session_label = _session_label(session.get("session_type")).upper()
    start_date = (
        time.strftime("%a %d %b %Y, %H:%M", time.localtime(session["started_at"]))
        if session.get("started_at")
        else ""
    )
    stint_summary = _stint_summary(stints, laps)
    meta = " · ".join(
        value
        for value in (
            start_date,
            stint_summary,
            f"calls {session.get('calls_mode') or 'unknown'}",
            f"session {uid}",
        )
        if value
    )
    header = (
        "<header class='session'><h1>"
        f"{_esc(track)} · {_esc(session_label)} · {len(laps)} LAPS</h1>"
        f"<div class='meta'>{_esc(meta)}</div></header>"
    )
    nav_links = []
    for index, (section_id, number, label) in enumerate(_SECTIONS):
        active_class = " class='on'" if index == 0 else ""
        nav_links.append(
            f"<a{active_class} href='#{_esc(section_id)}'>"
            f"<span class='n'>{_esc(number)}</span>{_esc(label)}</a>"
        )
    nav = (
        "<nav class='agenda' aria-label='Sections'>"
        "<div class='brand'><b>pitwall</b> · DEBRIEF</div>"
        + "".join(nav_links)
        + "<div class='sp'></div>"
        + _provenance(session, calls, settings)
        + "</nav>"
    )
    sections = (
        _summary_section(session, laps, pits, pit_laps, calls, grades)
        + _pace_section(laps, stints, pit_laps)
        + _sector_section(laps)
        + _tyres_section(laps, settings)
        + _strategy_section(calls, pits, pit_laps, grades, outcomes)
        + _radio_section(calls, laps, pit_laps, grades, outcomes, inputs, editable=editable)
        + _incidents_section(laps)
        + _actions_section(db, uid, settings, session, grades)
    )
    scripts = _NAV_SCRIPT + (_grade_script(uid) if editable else "")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Session {_esc(uid)} · Pitwall debrief</title>{_STYLE}</head><body>"
        + nav
        + "<main>"
        + header
        + sections
        + "</main>"
        + scripts
        + "</body></html>"
    )


def render_debrief_index(db: Database, *, limit: int = 100) -> str:
    """HTML list of stored sessions, newest first, each linking to its debrief."""
    rows = db.sessions()[::-1][:limit]
    body_rows = []
    for row in rows:
        uid = int(row["uid"])
        track_id = row.get("track_id")
        track = f"{_track_name(track_id)} · {track_id if track_id is not None else '—'}"
        path = str(row.get("recording_path") or "")
        recording = os.path.basename(path) if path else "not linked"
        start = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row["started_at"]))
            if row.get("started_at")
            else "—"
        )
        session_link = _Markup(f"<a href='/debrief/{_esc(uid)}'>{_esc(uid)}</a>")
        is_synthetic = bool(row.get("synthetic")) or is_synthetic_uid(uid)
        origin: object = "-"
        if is_synthetic:
            derived_from = str(row.get("derived_from") or "")
            origin = _Markup(
                str(_chip("warn", "synthetic"))
                + (f"<br>derived from {_esc(derived_from)}" if derived_from else "")
            )
        body_rows.append(
            (
                session_link,
                start,
                track,
                _session_label(row.get("session_type")),
                len(db.laps_for(uid)),
                origin,
                recording,
            )
        )
    table = (
        _table(
            ("Session", "Start", "Track", "Type", "Laps", "Origin", "Recording"),
            body_rows,
            "sessions and laps",
        )
        if rows
        else "<p>No sessions stored yet.</p>"
    )
    content = _card("SESSIONS", table)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Sessions · Pitwall debrief</title>{_STYLE}</head><body class='index'>"
        "<main><header class='session'><div class='brand'><b>pitwall</b> · SESSIONS</div>"
        "<div class='meta'>Newest first · "
        "<a href='/debrief/latest'>Open the latest debrief</a></div></header>"
        + content
        + "</main></body></html>"
    )
