"""Replay tests for the F1 26 overtake and energy rules (docs/18): CT2 streams
drive overtake_available / overtake_active through detection and activation."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
import yaml

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.loader import DEFAULTS_DIR, ConfigStore
from pitwall.config.models import PolicySettings
from pitwall.engine import build_engine, run_replay
from pitwall.rules.engine import Candidate, Rule
from pitwall.state.session import Snapshot
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream

pytestmark = [pytest.mark.slow, pytest.mark.replay]

ON_BUDGET = {"thresholds": {"energy_over_tolerance_j": 3_000_000}}
ATTACK_OK = {
    "mindset": {"active": "aggressive"},
    "thresholds": {"energy_over_tolerance_j": 3_000_000},
}


def _replay(
    tmp: Path, spec: RaceSpec, overrides: dict | None = None
) -> tuple[list[Call], list[dict]]:
    tmp.mkdir(exist_ok=True)
    path = write_packet_stream(tmp / "race.f1bin", race_stream(spec))
    log = tmp / "decisions.jsonl"
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=Database(":memory:"),
        decision_log_path=log,
        overrides=overrides,
    )
    _, calls = asyncio.run(run_replay(path, engine, None))
    engine.dispatcher.log.flush()
    rows = [json.loads(line) for line in log.read_text().splitlines() if line]
    return calls, rows


def _fired(rows: list[dict], rule_id: str) -> list[dict]:
    return [r for r in rows if r["rule_id"] == rule_id and r["outcome"] == "fired"]


def test_overtake_earned_fires_inside_the_detection_window(tmp_path: Path) -> None:
    _, rows = _replay(
        tmp_path,
        RaceSpec(laps=2, base_ms=100_000, ct2=True, overtake_detect_m=2000, gap_ahead_s=0.5),
    )
    fired = _fired(rows, "overtake_earned")
    assert len(fired) == 1 and fired[0]["lap"] == 1
    assert "NORRIS" in fired[0]["text"] and "0.5" in fired[0]["text"]

    _, far = _replay(
        tmp_path / "far",
        RaceSpec(laps=2, base_ms=100_000, ct2=True, overtake_detect_m=2000, gap_ahead_s=1.5),
    )
    assert _fired(far, "overtake_earned") == []


def test_overtake_earned_rearms_after_the_gap_returns(tmp_path: Path) -> None:
    _, rows = _replay(
        tmp_path,
        RaceSpec(
            laps=3,
            base_ms=100_000,
            ct2=True,
            overtake_detect_m=2000,
            gap_ahead_s=1.5,
            gap_ahead_from_lap=(3, 0.8),
        ),
    )
    fired = _fired(rows, "overtake_earned")
    assert [r["lap"] for r in fired] == [3]


def test_overtake_lost_fires_when_availability_drops_while_active(tmp_path: Path) -> None:
    _, rows = _replay(
        tmp_path,
        RaceSpec(
            laps=4,
            base_ms=30_000,
            ct2=True,
            overtake_detect_m=2000,
            gap_ahead_s=0.8,
            gap_ahead_from_lap=(3, 1.5),
        ),
    )
    fired = _fired(rows, "overtake_lost")
    assert len(fired) == 1 and fired[0]["lap"] == 3

    _, close = _replay(
        tmp_path / "close",
        RaceSpec(laps=3, base_ms=30_000, ct2=True, overtake_detect_m=2000, gap_ahead_s=0.8),
    )
    assert _fired(close, "overtake_lost") == []


def test_overtake_mode_needs_energy_aero_zone_and_active_overtake(tmp_path: Path) -> None:
    spec = dict(
        laps=4,
        base_ms=100_000,
        ct2=True,
        overtake_detect_m=2000,
        aero_zones_m=((3000, 3400),),
        gap_ahead_s=0.8,
    )
    _, rows = _replay(tmp_path, RaceSpec(**spec), ATTACK_OK)  # type: ignore[arg-type]
    assert _fired(rows, "overtake_mode")

    _, no_zone = _replay(
        tmp_path / "nozone",
        RaceSpec(**{**spec, "aero_zones_m": ()}),
        ATTACK_OK,  # type: ignore[arg-type]
    )
    assert _fired(no_zone, "overtake_mode") == []

    _, no_overtake = _replay(
        tmp_path / "noot",
        RaceSpec(**{**spec, "gap_ahead_s": 1.4}),
        ATTACK_OK,  # type: ignore[arg-type]
    )
    assert _fired(no_overtake, "overtake_mode") == []


def test_energy_burst_fires_on_low_store_while_attacking(tmp_path: Path) -> None:
    spec = dict(laps=5, base_ms=30_000, gap_ahead_s=0.8)
    _, rows = _replay(tmp_path, RaceSpec(**spec, ers_store_j=1_000_000), ON_BUDGET)  # type: ignore[arg-type]
    fired = _fired(rows, "energy_burst")
    assert fired and all(r["lap"] >= 2 for r in fired)

    _, full = _replay(
        tmp_path / "full",
        RaceSpec(**spec, ers_store_j=3_900_000),
        ON_BUDGET,  # type: ignore[arg-type]
    )
    assert _fired(full, "energy_burst") == []


def test_energy_charge_for_battle_fires_while_catching_on_low_store(tmp_path: Path) -> None:
    _, rows = _replay(
        tmp_path,
        RaceSpec(
            laps=6,
            base_ms=30_000,
            ers_store_j=1_000_000,
            gap_ahead_s=2.6,
            gap_ahead_from_lap=(3, 1.9),
        ),
        ON_BUDGET,
    )
    assert _fired(rows, "energy_charge_for_battle")

    _, steady = _replay(
        tmp_path / "steady",
        RaceSpec(laps=6, base_ms=30_000, ers_store_j=1_000_000, gap_ahead_s=2.6),
        ON_BUDGET,
    )
    assert _fired(steady, "energy_charge_for_battle") == []


def test_queued_energy_call_is_superseded_by_overtake_earned() -> None:
    rules = {r.id: Rule(r) for r in ConfigStore().current().rules}
    buf = io.StringIO()
    dispatcher = Dispatcher(
        PolicySettings(),
        VirtualClock(),
        decision_log=DecisionLog(fp=buf),
        sinks=[],
    )
    snap = Snapshot(now=0.0)

    def cand(rule_id: str) -> Candidate:
        defn = rules[rule_id].defn
        c = Candidate(
            rule=rules[rule_id],
            text=rule_id,
            priority=defn.priority,
            tags=[],
            still_true=None,
            inputs={},
            trigger_t=0.0,
        )
        c.current = lambda snap: True  # still holds when the rival fires
        return c

    dispatcher.submit([cand("energy_charge_for_battle")], snap)
    dispatcher.submit([cand("overtake_earned")], snap)

    assert [item.call.rule_id for item in dispatcher._queue] == ["overtake_earned"]
    outcomes = {
        (r["rule_id"], r["outcome"], r.get("suppressed_by"))
        for r in (json.loads(line) for line in buf.getvalue().splitlines() if line)
    }
    assert ("energy_charge_for_battle", "suppressed", "superseded") in outcomes


def test_no_drs_left_in_race_rule_phrasing() -> None:
    data = yaml.safe_load((DEFAULTS_DIR / "rules" / "race.yaml").read_text())
    texts: list[str] = []

    def collect(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("say", "severity"):
                    collect(value)
                elif isinstance(value, dict | list):
                    collect(value)
        elif isinstance(node, list):
            texts.extend(x for x in node if isinstance(x, str))
            for x in node:
                if isinstance(x, dict | list):
                    collect(x)

    collect(data["rules"])
    assert texts
    assert [t for t in texts if "DRS" in t.upper()] == []
