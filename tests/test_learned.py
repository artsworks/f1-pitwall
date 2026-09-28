from __future__ import annotations

import json

from pitwall.cli import main
from pitwall.config.loader import ConfigStore
from pitwall.learned import format_learned, learned_state
from pitwall.store.db import Database
from pitwall.strategy.battle import PASS_COMPOUND, PASS_DRS
from pitwall.tune import COOLDOWN_PREFIX, TUNE_COMPOUND, TUNE_TRACK


def test_learned_state_and_formatter_show_sources_and_feedback(tmp_path) -> None:
    db = Database(tmp_path / "learn.sqlite")
    uid = 702
    db.upsert_session(uid, track_id=7, session_type=15, started_at=10.0)
    db.set_param(7, 17, "deg_ms_per_lap", 95.0, 10.0)
    db.set_param(7, 17, "base_ms", 90_100.0, 10.0)
    db.set_param(7, 17, "fuel_ms_per_lap", 52.0, 10.0)
    db.set_param(7, 0, "fuel_kg_per_lap", 1.8, 5.0)
    db.set_param(7, 0, "pit_loss_green_ms", 25_000.0, 5.0)
    db.set_param(7, 17, "thermal_lo_c", 85.0, 5.0)
    db.set_param(7, 17, "thermal_hi_c", 100.0, 5.0)
    db.set_param(7, 0, "energy_deployed_j_p50", 400_000.0, 5.0)
    db.set_param(7, PASS_COMPOUND, PASS_DRS, 0.6, 5.0)
    db.insert_call(
        uid,
        {
            "outcome": "driver_input",
            "item_id": "pit",
            "kind": "opinion",
            "inputs": {"case": "ack"},
        },
    )
    db.insert_call(
        uid,
        {
            "outcome": "driver_input",
            "item_id": "pit",
            "kind": "opinion",
            "inputs": {"case": "neg"},
        },
    )
    db.insert_call(uid, {"outcome": "fired", "call_id": "c1", "rule_id": "box_now"})
    db.grade_call(uid, "c1", "box_now", "noise")
    db.set_param(
        TUNE_TRACK,
        TUNE_COMPOUND,
        COOLDOWN_PREFIX + "box_now",
        4.0,
        3.0,
    )
    settings = ConfigStore().current()

    state = learned_state(db, settings)
    track = state["tracks"][0]
    text = format_learned(state)

    assert track["session_count"] == 1
    assert track["ingested_count"] == 0
    assert track["compounds"][17]["deg_ms_per_lap"]["source"] == "learned"
    assert track["compounds"][17]["deg_ms_per_lap"]["delta_to_default"] != 0
    assert track["compounds"][17]["base_ms"]["source"] == "learned"
    assert track["compounds"][17]["fuel_ms_per_lap"]["source"] == "learned"
    assert track["fuel_kg_per_lap"]["source"] == "learned"
    assert track["pit_loss_ms"]["green"]["source"] == "learned"
    assert track["thermal"]["compounds"][17]["lo"]["source"] == "learned"
    assert track["energy"]["energy_deployed_j_p50"]["source"] == "learned"
    assert track["battle_priors"][PASS_DRS]["source"] == "learned"
    assert track["driver_input_ack_neg_by_rule"] == [
        {"rule_id": "pit", "ack_count": 1, "neg_count": 1}
    ]
    assert state["tuned_cooldowns"][0]["grade_count"] == 1
    assert "Track 7" in text
    assert "feedback pit: ack=1 neg=1" in text
    assert "pit loss ms:" in text and "thermal compound 17" in text
    assert "energy energy_deployed_j_p50" in text and f"battle {PASS_DRS}" in text


def test_stats_learned_json_and_empty_doctor_are_safe(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "empty.sqlite"
    assert main(["stats", "--learned", "--db", str(db_path), "--json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["tracks"] == []

    populated_path = tmp_path / "populated.sqlite"
    populated = Database(populated_path)
    populated.upsert_session(703, track_id=7, session_type=15)
    populated.set_param(7, 17, "deg_ms_per_lap", 100.0, 5.0)
    populated.close()
    assert main(["stats", "--learned", "--db", str(populated_path), "--json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["tracks"][0]["track_id"] == 7
    assert rendered["tracks"][0]["compounds"]["17"]["deg_ms_per_lap"]["source"] == "learned"

    import pitwall.doctor
    import pitwall.store.db

    monkeypatch.setattr(pitwall.doctor, "run_doctor", lambda seconds: 7)
    empty_db = Database(":memory:")
    monkeypatch.setattr(pitwall.store.db, "open_configured", lambda settings: empty_db)
    assert main(["doctor", "--seconds", "0"]) == 7
    assert "no persisted track learning" in capsys.readouterr().out
    empty_db.close()

    monkeypatch.setattr(pitwall.store.db, "open_configured", lambda settings: None)

    assert main(["doctor", "--seconds", "0"]) == 7
    output = capsys.readouterr().out
    assert "learned state:" in output
    assert "no persisted track learning" in output
