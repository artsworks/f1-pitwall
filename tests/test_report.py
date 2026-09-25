from __future__ import annotations

import zipfile
from pathlib import Path

from pitwall.cli import build_parser
from pitwall.store.db import Database

from .synth import out_lap_scenario, write_packet_stream


def test_report_bundle(tmp_path: Path, monkeypatch) -> None:
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    rec = write_packet_stream(rec_dir / "sess.f1bin", out_lap_scenario())
    (rec_dir / "default.decisions.jsonl").write_text('{"outcome":"fired"}\n')
    db_path = tmp_path / "test.sqlite"
    db = Database(db_path)
    db.upsert_session(1)
    db.insert_call(1, {"outcome": "fired", "rule_id": "x", "t": 1.0})
    db.grade_call(1, "c-1", "x", "good")
    db.close()

    out = tmp_path / "rep.zip"
    args = build_parser().parse_args(
        [
            "report",
            "--recording",
            str(rec),
            "-o",
            str(out),
        ]
    )
    # point persistence + recordings at the tmp locations
    monkeypatch.setenv("PITWALL_PROFILE", str(tmp_path / "no-profile.yaml"))
    import pitwall.cli as cli_mod

    # ConfigStore picks up settings dir paths; override via overrides on the
    # store is cumbersome -> patch open_configured & recording dir indirectly.
    orig_load = cli_mod.ConfigStore

    class _Store(orig_load):  # type: ignore[misc]
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            super().__init__(
                overrides={
                    "persistence": {"enabled": True, "path": str(db_path)},
                    "recording": {"directory": str(rec_dir)},
                }
            )

    monkeypatch.setattr(cli_mod, "ConfigStore", _Store)
    rc = args.func(args)
    assert rc == 0 and out.exists()
    names = set(zipfile.ZipFile(out).namelist())
    assert "sess.f1bin" in names
    assert "config.yaml" in names and "system.json" in names
    assert "calls.json" in names and "grades.json" in names
