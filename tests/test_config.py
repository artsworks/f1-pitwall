from __future__ import annotations

from pathlib import Path

import yaml

from pitwall.config.loader import ConfigStore
from pitwall.config.models import Settings


def test_defaults_load() -> None:
    store = ConfigStore()
    s = store.current()
    assert s.connection.udp_port == 20777
    assert s.engine.tick_hz == 10
    assert s.thresholds["tyre_inner_cold_c"] == 80
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
