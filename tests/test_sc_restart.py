from __future__ import annotations

from pathlib import Path

import pytest

from pitwall.audio.dispatcher import Call
from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId
from pitwall.state.session import SessionState

from .race_synth import RaceSpec, replay_race
from .synth import pack_packet

pytestmark = pytest.mark.replay


def _replay(tmp_path: Path, spec: RaceSpec) -> tuple[list[Call], list[dict]]:
    return replay_race(tmp_path, spec)


def test_cold_sc_ending_replaces_plain_ending_call(tmp_path: Path) -> None:
    calls, rows = _replay(
        tmp_path,
        RaceSpec(
            laps=6,
            sc_laps=(3, 4),
            tyre_inner_profile=(95, 95, 95, 55, 95, 95),
        ),
    )

    cold_call = next(call for call in calls if call.rule_id == "sc_ending_tyres_cold")
    assert cold_call.lap == 4
    assert "55.0" in cold_call.text
    assert "sc_ending" not in {call.rule_id for call in calls}
    assert any(
        row["rule_id"] == "sc_ending"
        and row["outcome"] == "suppressed"
        and row["suppressed_by"] == "superseded"
        for row in rows
    )


def test_warm_sc_ending_keeps_plain_call(tmp_path: Path) -> None:
    calls, _ = _replay(
        tmp_path,
        RaceSpec(laps=6, sc_laps=(3, 4), tyre_inner_profile=(95,) * 6),
    )

    assert "sc_ending" in {call.rule_id for call in calls}
    assert "sc_ending_tyres_cold" not in {call.rule_id for call in calls}


def test_cold_vsc_ending_uses_vsc_call(tmp_path: Path) -> None:
    calls, _ = _replay(
        tmp_path,
        RaceSpec(
            laps=6,
            sc_laps=(3, 4),
            vsc=True,
            tyre_inner_profile=(95, 95, 95, 55, 95, 95),
        ),
    )
    rule_ids = {call.rule_id for call in calls}

    assert "vsc_ending_tyres_cold" in rule_ids
    assert "vsc_ending" not in rule_ids
    assert (
        not {
            "sc_deployed",
            "sc_ending",
            "sc_ending_tyres_cold",
            "sc_restart",
        }
        & rule_ids
    )


@pytest.mark.parametrize(
    ("vsc", "plain_rule", "cold_rule"),
    [
        (False, "sc_ending", "sc_ending_tyres_cold"),
        (True, "vsc_ending", "vsc_ending_tyres_cold"),
    ],
    ids=("sc", "vsc"),
)
def test_late_cold_tyre_does_not_repeat_ending_call(
    tmp_path: Path,
    vsc: bool,
    plain_rule: str,
    cold_rule: str,
) -> None:
    calls, _ = _replay(
        tmp_path,
        RaceSpec(
            laps=6,
            sc_laps=(3, 4),
            vsc=vsc,
            tyre_inner_profile=(95, 95, 95, 95, 55, 95),
        ),
    )
    ending_calls = [call for call in calls if call.rule_id in (plain_rule, cold_rule)]
    plain_call = next(call for call in ending_calls if call.rule_id == plain_rule)

    assert plain_call.lap == 4
    assert len(ending_calls) == 1, [call.rule_id for call in ending_calls]


def test_sc_negative_delta_fires_once_with_value(tmp_path: Path) -> None:
    calls, _ = _replay(
        tmp_path,
        RaceSpec(laps=6, sc_laps=(4, 4), sc_delta_s=-0.6),
    )
    delta_calls = [call for call in calls if call.rule_id == "sc_delta_negative"]

    assert len(delta_calls) == 1
    assert "-0.6" in delta_calls[0].text


def test_sc_delta_warning_requires_negative_delta_during_neutralisation(tmp_path: Path) -> None:
    positive_calls, _ = _replay(
        tmp_path,
        RaceSpec(laps=6, sc_laps=(4, 4), sc_delta_s=0.5),
    )
    green_negative_calls, _ = _replay(
        tmp_path,
        RaceSpec(laps=6, sc_delta_s=-0.6),
    )

    assert "sc_delta_negative" not in {call.rule_id for call in positive_calls}
    assert "sc_delta_negative" not in {call.rule_id for call in green_negative_calls}


def test_snapshot_uses_player_lap_data_safety_car_delta() -> None:
    ingest = Ingest()
    state = SessionState()
    state.register(ingest)
    ingest.on_datagram(
        pack_packet(
            PacketId.LAP_DATA,
            {"cars": {0: {"safety_car_delta": -0.6}}},
        ),
        0.0,
    )

    assert state.snapshot(0.0).safety_car_delta_s == pytest.approx(-0.6)
