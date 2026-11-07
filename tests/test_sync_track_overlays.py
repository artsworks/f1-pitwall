from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).parent.parent / "scripts" / "sync_track_overlays.py"
spec = importlib.util.spec_from_file_location("sync_track_overlays", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def test_packaged_files_rerender_byte_identical() -> None:
    for path in sorted(sync.TRACKS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        assert sync.render(data).encode() == path.read_bytes().replace(b"\r\n", b"\n"), path.name


def test_merge_keeps_hand_fields_and_packaged_only_threshold_keys() -> None:
    packaged = {
        "track_id": 7,
        "name": "silverstone",
        "pit_loss_s": {"green": 21.5},
        "fuel_kg_per_lap": 1.2,
        "deg_ms_per_lap": {16: 110},
        "thresholds": {"energy_over_tolerance_j": 600000, "custom_key": 3},
    }
    learned = {
        "fuel_kg_per_lap": 1.3,
        "thresholds": {"energy_over_tolerance_j": 500000},
    }
    merged, warnings = sync.merge_overlay(packaged, learned, track_id=7)
    assert warnings == []
    assert merged["pit_loss_s"] == {"green": 21.5}
    assert merged["deg_ms_per_lap"] == {16: 110}
    assert merged["fuel_kg_per_lap"] == 1.3
    assert merged["thresholds"] == {"energy_over_tolerance_j": 500000, "custom_key": 3}


def test_int_keys_converts_digit_strings() -> None:
    learned = sync._int_keys({"thresholds": {"cold": {"16": 85.0}}, "fuel": 1})
    assert learned == {"thresholds": {"cold": {16: 85.0}}, "fuel": 1}


def test_deg_is_warned_and_not_copied() -> None:
    packaged = {"track_id": 1, "deg_ms_per_lap": {16: 100}}
    merged, warnings = sync.merge_overlay(
        packaged, {"deg_ms_per_lap": {16: 50}, "fuel_kg_per_lap": 1.0}, track_id=1
    )
    assert merged["deg_ms_per_lap"] == {16: 100}
    assert any("deg_ms_per_lap" in w for w in warnings)


def test_track_name_from_map_and_override() -> None:
    assert sync.track_name(13, {}) == "suzuka"
    assert sync.track_name(99, {99: "custom_venue"}) == "custom_venue"
    with pytest.raises(ValueError, match="--name"):
        sync.track_name(99, {})


def test_new_overlay_skips_deg() -> None:
    overlay = sync.new_overlay(2, "shanghai", {"fuel_kg_per_lap": 1.2, "deg_ms_per_lap": {16: 50}})
    assert overlay == {"track_id": 2, "name": "shanghai", "fuel_kg_per_lap": 1.2}


def test_dirty_tree_aborts_without_tty_or_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sync,
        "_git",
        lambda *a: subprocess.CompletedProcess(a, 0, stdout=" M foo.py\n", stderr=""),
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert sync.check_clean_tree(yes=False) is False
    assert sync.check_clean_tree(yes=True) is True


def test_load_pack_reads_track_overlays(tmp_path: Path) -> None:
    pack = tmp_path / "pack.json"
    pack.write_text(
        json.dumps(
            {
                "written_at": "2026-11-05T23:44:55+11:00",
                "track_overlays": {
                    "-1": {"fuel_kg_per_lap": 1.0},
                    "13": {"fuel_kg_per_lap": 1.3, "thresholds": {"e": {"16": 1}}},
                },
            }
        )
    )
    date, overlays = sync.load_pack(pack)
    assert date == "2026-11-05"
    assert overlays[-1] == {"fuel_kg_per_lap": 1.0}
    assert overlays[13]["thresholds"] == {"e": {16: 1}}


def test_verify_written_keeps_packaged_only_threshold_keys(tmp_path: Path) -> None:
    packaged = {
        "track_id": 7,
        "thresholds": {"energy_over_tolerance_j": 600000, "packaged_only": 3},
    }
    learned = {
        "fuel_kg_per_lap": 1.3,
        "thresholds": {"energy_over_tolerance_j": 500000},
    }
    merged, _ = sync.merge_overlay(packaged, learned, track_id=7)
    path = tmp_path / "7.yaml"
    path.write_text(sync.render(merged))
    written = yaml.safe_load(path.read_text())
    assert written["thresholds"] == {"energy_over_tolerance_j": 500000, "packaged_only": 3}
    assert sync.verify_written(written, learned) == []
    assert sync.verify_written(written, {"thresholds": {"energy_over_tolerance_j": 1}}) == [
        "thresholds.energy_over_tolerance_j"
    ]


def test_load_overlays_home_dir_wins_and_warns(tmp_path: Path) -> None:
    (tmp_path / "7.yaml").write_text("fuel_kg_per_lap: 1.9\n")
    overlays, warnings = sync.load_overlays({7: {"fuel_kg_per_lap": 1.2}}, tmp_path)
    assert overlays == {7: {"fuel_kg_per_lap": 1.9}}
    assert len(warnings) == 1 and "mismatch" in warnings[0]
    overlays, warnings = sync.load_overlays({7: {"fuel_kg_per_lap": 1.2}}, tmp_path / "empty")
    assert overlays == {7: {"fuel_kg_per_lap": 1.2}}
    assert warnings == []
