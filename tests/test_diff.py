from __future__ import annotations

import shutil
from pathlib import Path

from pitwall.diff import format_diff, run_diff

from .synth import out_lap_scenario, write_packet_stream

DEFAULT_RULES = Path(__file__).parent.parent / "src/pitwall/config/defaults/rules"


def _copy_rules(dst: Path, mutate: str | None = None) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for f in DEFAULT_RULES.glob("*.yaml"):
        shutil.copy(f, dst / f.name)
    if mutate:
        shared = dst / "shared.yaml"
        text = shared.read_text()
        assert mutate in text
        shared.write_text(text.replace(mutate, "False"))
    return dst


def test_diff_rules_dirs(tmp_path: Path) -> None:
    rec = write_packet_stream(tmp_path / "r.f1bin", out_lap_scenario())
    a_dir = _copy_rules(tmp_path / "a")
    b_dir = _copy_rules(tmp_path / "b", mutate="phase == 'out_lap' and sector == 2")
    result = run_diff([rec], a_dir, b_dir)
    f = result["files"][0]
    only_a_ids = {r["rule_id"] for r in f["only_a"]}
    assert "out_lap_s3_tyres_cold" in only_a_ids
    assert f["summary"]["totals"]["only_a"] >= 1
    out = format_diff(result)
    assert "only-A" in out and "out_lap_s3_tyres_cold" in out


def test_diff_json_shape_and_corpus(tmp_path: Path) -> None:
    rec1 = write_packet_stream(tmp_path / "r1.f1bin", out_lap_scenario())
    rec2 = write_packet_stream(tmp_path / "r2.f1bin", out_lap_scenario())
    a_dir = _copy_rules(tmp_path / "a")
    b_dir = _copy_rules(tmp_path / "b", mutate="phase == 'out_lap' and sector == 2")
    result = run_diff([rec1, rec2], a_dir, b_dir)
    assert len(result["files"]) == 2
    t = result["totals"]
    assert t["only_a"] >= 2  # same miss under B in both recordings
    per_file = result["files"][0]["summary"]
    assert "per_lap" in per_file and "per_priority" in per_file
    assert "per_rule" in per_file and "totals" in per_file
