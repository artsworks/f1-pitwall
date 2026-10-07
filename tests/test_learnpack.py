from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from pitwall.cli import main
from pitwall.config.loader import ConfigStore
from pitwall.digest import quality_trend, startup_scorecard
from pitwall.learnpack import (
    LATEST_NAME,
    LEDGER_NAME,
    PACK_VERSION,
    merged_sessions,
    pack_track_minutes,
    read_ledger,
    restore_pack,
    write_pack,
)
from pitwall.maintenance import LEARN_VERSION, maintain
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database, ModelParam


def _lap(lap_num: int, lap_time_ms: int) -> LapSummary:
    return LapSummary(lap_num, lap_time_ms, 30_000, 30_000, 17, 1, 4.0, True)


def test_write_pack_creates_latest_dated_and_ledger_files_without_temps(tmp_path: Path) -> None:
    db = Database(":memory:")
    uid = (1 << 63) + 321
    db.upsert_session(uid, track_id=7, session_type=15, started_at=10.0)
    db.insert_lap(uid, 0, _lap(1, 90_000))
    db.set_param(7, 17, "deg_ms_per_lap", 82.5, 4.0)
    db.grade_call(uid, "call-1", "tyre_life", "good", source="press")

    overlay_dir = tmp_path / "overlays"
    overlay_dir.mkdir()
    (overlay_dir / "7.yaml").write_text(
        yaml.safe_dump({"track_id": 7, "track": {"base_pace_ms": 90_000}}),
        encoding="utf-8",
    )
    (overlay_dir / "broken.yaml").write_text("track: [", encoding="utf-8")
    pack_dir = tmp_path / "learnings"

    latest = write_pack(
        db,
        pack_dir,
        now=datetime(2025, 4, 5, 12),
        overlay_dir=overlay_dir,
    )

    assert latest == pack_dir / LATEST_NAME
    assert latest.is_file()
    assert (pack_dir / "learning-2025-04-05.json").is_file()
    ledger = read_ledger(pack_dir / LEDGER_NAME)
    assert ledger[uid]["uid"] == uid
    assert ledger[uid]["ms"] == 90_000
    pack = json.loads(latest.read_text(encoding="utf-8"))
    assert pack["pack_version"] == PACK_VERSION
    assert pack["db_user_version"] > 0
    assert pack["track_minutes"] == {"minutes": 1.5, "laps": 1, "sessions": 1}
    assert pack["model_params"][0]["name"] == "deg_ms_per_lap"
    assert pack["call_grades"][0]["session_uid"] == uid
    assert pack["call_grades"][0]["source"] == "press"
    assert pack["maintenance"] == {}
    assert pack["track_overlays"] == {"7": {"track_id": 7, "track": {"base_pace_ms": 90_000}}}
    assert not [path for path in pack_dir.iterdir() if path.suffix == ".tmp"]


def test_write_pack_keeps_newest_dated_files(tmp_path: Path) -> None:
    db = Database(":memory:")
    pack_dir = tmp_path / "learnings"
    pack_dir.mkdir()
    first_day = datetime(2024, 1, 1)
    for day in range(32):
        date = (first_day + timedelta(days=day)).date().isoformat()
        (pack_dir / f"learning-{date}.json").write_text("{}", encoding="utf-8")

    write_pack(db, pack_dir, keep_days=30, now=datetime(2024, 2, 1))

    dated = sorted(pack_dir.glob("learning-????-??-??.json"))
    assert len(dated) == 30
    assert pack_dir / "learning-2024-02-01.json" in dated


def test_read_ledger_skips_bad_lines_and_rows_without_integer_uids(tmp_path: Path) -> None:
    ledger = tmp_path / LEDGER_NAME
    ledger.write_text(
        '\nnot json\n{"uid":"7","ms":1}\n{"ms":2}\n{"uid":7,"ms":60000}\n',
        encoding="utf-8",
    )

    assert read_ledger(ledger) == {7: {"uid": 7, "ms": 60_000}}


def test_merged_sessions_uses_max_ms_and_counts_ledger_only_rows(tmp_path: Path) -> None:
    db = Database(":memory:")
    db_uid = (1 << 63) + 77
    db.upsert_session(db_uid, track_id=8, session_type=15, started_at=20.0)
    db.insert_lap(db_uid, 0, _lap(1, 90_000))
    db.insert_lap(db_uid, 0, _lap(1, 100_000))
    db.insert_lap(db_uid, 0, _lap(2, 120_000))

    pack_dir = tmp_path / "learnings"
    pack_dir.mkdir()
    (pack_dir / LEDGER_NAME).write_text(
        '{"uid":55,"started_at":10,"track_id":3,"session_type":10,"laps":2,"ms":120000}\n'
        f'{{"uid":{db_uid},"started_at":19,"track_id":8,"session_type":15,"laps":1,"ms":180000,'
        '"quality":{"fired":1}}\n'
        "{broken\n",
        encoding="utf-8",
    )

    sessions = merged_sessions(db, pack_dir)
    assert set(sessions) == {55, db_uid}
    assert sessions[db_uid] == {
        "uid": db_uid,
        "started_at": 20.0,
        "track_id": 8,
        "session_type": 15,
        "laps": 2,
        "ms": 220_000,
        "quality": {"fired": 1},
    }
    assert pack_track_minutes(db, pack_dir) == {"minutes": 5.7, "laps": 4, "sessions": 2}


def test_startup_scorecard_counts_ledger_only_sessions(tmp_path: Path) -> None:
    db = Database(":memory:")
    pack_dir = tmp_path / "learnings"
    pack_dir.mkdir()
    (pack_dir / LEDGER_NAME).write_text(
        '{"uid":88,"started_at":10,"track_id":7,"session_type":15,"laps":2,"ms":120000}\n',
        encoding="utf-8",
    )

    assert startup_scorecard(db, pack_dir) == (
        "pitwall: 2 track minutes over 1 sessions · call quality n/a"
    )


def test_restored_quality_history_feeds_trend_and_startup(tmp_path: Path) -> None:
    source = Database(":memory:")
    uids = (301, 302, 303)
    for index, uid in enumerate(uids):
        source.upsert_session(uid, track_id=7, session_type=15, started_at=100.0 + index)
        if index < 2:
            source.insert_lap(uid, 0, _lap(1, 90_000))
        call_id = f"call-{uid}"
        source.insert_call(
            uid,
            {"outcome": "fired", "call_id": call_id, "rule_id": "tyre_life", "t": 10.0},
        )
        source.grade_call(uid, call_id, "tyre_life", "good")
    pack_path = write_pack(source, tmp_path / "source-pack")

    target = Database(":memory:")
    ledger_dir = tmp_path / "restored-ledger"
    restore_pack(target, pack_path, ledger_dir)
    target.upsert_session(uids[0], track_id=7, session_type=15, started_at=100.0)
    target.insert_call(
        uids[0],
        {
            "outcome": "fired",
            "call_id": f"call-{uids[0]}",
            "rule_id": "tyre_life",
            "t": 10.0,
        },
    )

    report = quality_trend(target, pack_dir=ledger_dir)

    assert [row["uid"] for row in report["sessions"]] == list(uids)
    assert sum(row["uid"] == uids[0] for row in report["sessions"]) == 1
    assert startup_scorecard(target, ledger_dir).endswith(
        "call quality 100% good (+0.0 over last 3)"
    )


def test_session_end_quality_refresh_only_recomputes_requested_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(":memory:")
    for uid in (401, 402):
        db.upsert_session(uid, track_id=7, session_type=15, started_at=float(uid))
        db.insert_lap(uid, 0, _lap(1, 90_000))
        db.insert_call(uid, {"outcome": "fired", "call_id": str(uid), "rule_id": "r", "t": 1.0})
    pack_dir = tmp_path / "learnings"
    pack_dir.mkdir()
    (pack_dir / LEDGER_NAME).write_text(
        '{"uid":402,"started_at":402,"track_id":7,"session_type":15,"laps":1,"ms":1000,'
        '"quality":{"fired":8}}\n',
        encoding="utf-8",
    )
    called: list[int] = []

    def quality(_db: Database, uid: int) -> dict[str, int]:
        called.append(uid)
        return {"fired": 1, "good": uid}

    monkeypatch.setattr("pitwall.learnpack.call_quality", quality)

    write_pack(db, pack_dir, refresh_quality=[401])

    ledger = read_ledger(pack_dir)
    assert called == [401]
    assert ledger[401]["quality"] == {"fired": 1, "good": 401}
    assert ledger[402]["quality"] == {"fired": 8}


def test_restore_merges_state_without_overwriting_stronger_values(tmp_path: Path) -> None:
    source = Database(":memory:")
    uid = (1 << 63) + 995
    source.set_param(7, 17, "low_weight", 4.5, 5.0)
    source.set_param(7, 17, "high_weight", 3.0, 5.0)
    source.set_param(7, 17, "new_param", 6.5, 3.0)
    source.quarantine_param(ModelParam(7, 17, "bad_value", 99.0, 2.0, 10.0), "outlier")
    source.grade_call(uid, "new-grade", "tyre_life", "good", source="press")
    source.grade_call(uid, "existing-grade", "tyre_life", "good", source="press")
    source.upsert_session(uid, track_id=7, session_type=15, started_at=11.0)
    source.insert_lap(uid, 0, _lap(1, 90_000))

    source_overlays = tmp_path / "source-overlays"
    source_overlays.mkdir()
    for track_id in (7, 8):
        (source_overlays / f"{track_id}.yaml").write_text(
            yaml.safe_dump({"track_id": track_id, "track": {"base_pace_ms": 90_000}}),
            encoding="utf-8",
        )
    pack_path = write_pack(
        source,
        tmp_path / "source-pack",
        now=datetime(2025, 4, 5),
        overlay_dir=source_overlays,
    )

    empty_target = Database(":memory:")
    empty_overlays = tmp_path / "empty-overlays"
    empty_counts = restore_pack(
        empty_target,
        pack_path,
        tmp_path / "empty-ledger",
        overlay_dir=empty_overlays,
    )
    assert empty_counts == {
        "model_params": 3,
        "model_params_quarantine": 1,
        "call_grades": 2,
        "maintenance": 0,
        "track_overlays": 2,
        "sessions": 1,
    }
    assert empty_target.get_param(7, 17, "new_param") is not None
    empty_grades = {row["call_id"]: row for row in empty_target.grades_for_session(uid)}
    assert empty_grades["new-grade"]["source"] == "press"
    assert empty_grades["existing-grade"]["source"] == "press"
    assert read_ledger(tmp_path / "empty-ledger" / LEDGER_NAME)[uid]["uid"] == uid

    target = Database(":memory:")
    target.set_param(7, 17, "low_weight", 1.0, 1.0)
    target.set_param(7, 17, "high_weight", 9.0, 10.0)
    target.grade_call(uid, "existing-grade", "tyre_life", "noise")
    target_overlays = tmp_path / "target-overlays"
    target_overlays.mkdir()
    existing_overlay = target_overlays / "7.yaml"
    existing_overlay.write_text("keep: true\n", encoding="utf-8")

    counts = restore_pack(
        target,
        pack_path,
        tmp_path / "restored",
        overlay_dir=target_overlays,
    )

    assert counts == {
        "model_params": 2,
        "model_params_quarantine": 1,
        "call_grades": 1,
        "maintenance": 0,
        "track_overlays": 1,
        "sessions": 1,
    }
    low = target.get_param(7, 17, "low_weight")
    assert low is not None and (low.value, low.weight) == (4.5, 5.0)
    high = target.get_param(7, 17, "high_weight")
    assert high is not None and (high.value, high.weight) == (9.0, 10.0)
    quarantine = target.quarantined_params()
    assert [(row["name"], row["reason"]) for row in quarantine] == [("bad_value", "outlier")]
    grades = {row["call_id"]: row for row in target.grades_for_session(uid)}
    assert grades["new-grade"]["grade"] == "good" and grades["new-grade"]["source"] == "press"
    assert grades["existing-grade"]["grade"] == "noise"
    restored_ledger = read_ledger(tmp_path / "restored" / LEDGER_NAME)
    assert restored_ledger[uid]["uid"] == uid
    assert yaml.safe_load(existing_overlay.read_text(encoding="utf-8")) == {"keep": True}
    assert yaml.safe_load((target_overlays / "8.yaml").read_text(encoding="utf-8")) == {
        "track_id": 8,
        "track": {"base_pace_ms": 90_000},
    }


def test_restore_keeps_stint_priors_active_after_maintenance(tmp_path: Path) -> None:
    source = Database(":memory:")
    source.set_param(7, 17, "deg_ms_per_lap", 120.0, 20.0)
    source.set_maintenance_version("learn_rebuild", LEARN_VERSION)
    pack_path = write_pack(source, tmp_path / "source-pack")

    target = Database(":memory:")
    counts = restore_pack(target, pack_path, tmp_path / "restored")
    report = maintain(target, ConfigStore().current(), refit=False)

    param = target.get_param(7, 17, "deg_ms_per_lap")
    assert counts["maintenance"] == 1
    assert report.quarantined == []
    assert param is not None and param.value == 120.0
    assert target.quarantined_params() == []


def test_restore_breaks_equal_weight_ties_by_updated_at(tmp_path: Path) -> None:
    source = Database(":memory:")
    source.set_param(7, 17, "deg_ms_per_lap", 110.0, 50.0)
    source_pack = write_pack(source, tmp_path / "source-pack")
    original = json.loads(source_pack.read_text(encoding="utf-8"))

    for name, pack_time, db_time, expected in (
        ("pack-newer", 200.0, 100.0, 110.0),
        ("db-newer", 100.0, 200.0, 80.0),
    ):
        pack = json.loads(json.dumps(original))
        pack["model_params"][0]["updated_at"] = pack_time
        pack_path = tmp_path / f"{name}.json"
        pack_path.write_text(json.dumps(pack), encoding="utf-8")

        target = Database(":memory:")
        target.set_param(7, 17, "deg_ms_per_lap", 80.0, 50.0)
        with target.transaction():
            target._conn.execute(
                "UPDATE model_params SET updated_at=? WHERE track_id=7 AND compound=17"
                " AND name='deg_ms_per_lap'",
                (db_time,),
            )

        restore_pack(target, pack_path, tmp_path / f"{name}-ledger")

        restored = target.get_param(7, 17, "deg_ms_per_lap")
        assert restored is not None and restored.value == expected


def test_restore_rejects_newer_pack_version(tmp_path: Path) -> None:
    path = tmp_path / "newer.json"
    path.write_text('{"pack_version":99}', encoding="utf-8")

    with pytest.raises(ValueError, match="newer than supported"):
        restore_pack(Database(":memory:"), path, tmp_path / "learnings")


def test_restore_cli_uses_configured_pack_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import pitwall.cli as cli_module

    source = Database(":memory:")
    pack_path = write_pack(source, tmp_path / "pack", now=datetime(2025, 4, 5))
    base_store = cli_module.ConfigStore

    class StoreWithPackDir(base_store):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(overrides={"learning": {"pack_dir": str(tmp_path / "ledger")}})

    monkeypatch.setattr(cli_module, "ConfigStore", StoreWithPackDir)
    db_path = tmp_path / "restored.sqlite"

    assert main(["restore", str(pack_path), "--db", str(db_path)]) == 0
    assert capsys.readouterr().out.startswith("restore: model_params=0")
    assert (tmp_path / "ledger" / LEDGER_NAME).is_file()
