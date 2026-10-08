from __future__ import annotations

from pathlib import Path

import yaml

from pitwall.calibrate import write_overlays
from pitwall.config.loader import ConfigStore
from pitwall.config.models import Settings, TrackOverlay


def test_defaults_load() -> None:
    store = ConfigStore()
    s = store.current()
    assert s.connection.udp_port == 20777
    assert s.engine.tick_hz == 10
    assert s.thresholds["tyre_inner_cold_c"] == 80
    quali_eliminated = s.thresholds["quali_eliminated"]
    assert isinstance(quali_eliminated, dict)
    assert all(type(value) is int for value in quali_eliminated.values())
    assert s.rules and s.rules[0].id == "out_lap_s3_tyres_cold"
    m = s.resolved_mindset()
    assert m["phrasing"] == "advisory"


def test_mindset_inherits() -> None:
    s = Settings.model_validate({"mindset": {"active": "aggressive"}})
    s.mindsets = yaml.safe_load(
        (Path(__file__).parent.parent / "src/pitwall/config/defaults/mindsets.yaml").read_text()
    )["mindsets"]
    m = s.resolved_mindset()
    assert m["thermal_warn_offset_c"] == 3
    assert m["pit_gain_min_s"] == 0.4  # overridden
    assert m["fuel_margin_laps"] == 0.10


def test_layered_overrides() -> None:
    store = ConfigStore(overrides={"policy": {"calls_per_lap": 2, "quiet": True}})
    s = store.current()
    assert s.policy.calls_per_lap == 2
    assert s.policy.quiet is True
    assert s.policy.min_gap_s == 3.0  # untouched defaults survive the merge


def test_invalid_override_keeps_last_good(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    profile = tmp_path / "profile.yaml"
    profile.write_text("policy:\n  calls_per_lap: 2\n")
    monkeypatch.setenv("PITWALL_PROFILE", str(profile))
    store = ConfigStore()
    assert store.current().policy.calls_per_lap == 2
    profile.write_text("policy:\n  verbosity: bogus\n")
    assert store.reload() is False
    assert store.last_error is not None
    assert store.current().policy.calls_per_lap == 2  # last good kept


def test_isolated_config_skips_profile(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    profile = tmp_path / "profile.yaml"
    profile.write_text("thresholds:\n  tyre_inner_cold_c: 42\n")
    monkeypatch.setenv("PITWALL_PROFILE", str(profile))

    configured = ConfigStore()
    isolated = ConfigStore(isolated=True)

    assert configured.current().thresholds["tyre_inner_cold_c"] == 42
    assert isolated.current().thresholds["tyre_inner_cold_c"] == 80
    assert profile not in isolated._sources()  # noqa: SLF001


def test_hash_stable() -> None:
    a = ConfigStore()
    b = ConfigStore()
    assert a.hash == b.hash
    assert len(a.hash) == 8
    c = ConfigStore(overrides={"policy": {"quiet": True}})
    assert c.hash != a.hash


def test_poll_detects_change(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    profile = tmp_path / "profile.yaml"
    profile.write_text("mindset:\n  active: balanced\n")
    monkeypatch.setenv("PITWALL_PROFILE", str(profile))
    store = ConfigStore()
    store._last_poll = -10  # noqa: SLF001
    profile.write_text("mindset:\n  active: aggressive\n")
    assert store.poll(0.0) is True


def test_calibration_overlay_round_trips_per_compound_thresholds(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    report = {
        "tracks": [
            {
                "track_id": 10,
                "compounds": {
                    17: {
                        "n_laps": 5,
                        "thermal": {
                            "n_laps": 5,
                            "thermal_lo_c": 92.5,
                            "thermal_hi_c": 97.5,
                        },
                    }
                },
            }
        ]
    }

    assert write_overlays(report, ConfigStore().current())

    store = ConfigStore()
    assert store.set_track(10) is True
    assert store.last_error is None
    assert store.current().track is not None
    assert store.current().thresholds["tyre_inner_cold_by_compound_c"][17] == 92.5


def test_packaged_track_overlays_validate() -> None:
    tracks_dir = Path(__file__).parent.parent / "src" / "pitwall" / "config" / "defaults" / "tracks"
    paths = sorted(tracks_dir.glob("*.yaml"))

    assert paths
    for path in paths:
        overlay = TrackOverlay.model_validate(yaml.safe_load(path.read_text()))
        assert overlay.track_id == int(path.stem)
