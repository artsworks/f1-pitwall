"""Track overlay loading: packaged defaults, user overrides, hash."""

from __future__ import annotations

from pathlib import Path

import pytest

from pitwall.config.loader import ConfigStore


def test_set_track_loads_overlay_and_changes_hash() -> None:
    store = ConfigStore()
    h0 = store.hash
    assert store.current().track is None
    assert store.set_track(7) is True
    track = store.current().track
    assert track is not None and track.name == "silverstone"
    assert track.pit_loss_s["green"] == 21.5
    assert store.hash != h0
    # same id again -> no change
    assert store.set_track(7) is False


def test_set_track_merges_overlay_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".pitwall" / "tracks").mkdir(parents=True)
    (home / ".pitwall" / "tracks" / "7.yaml").write_text(
        "pit_loss_s: {green: 19.0}\nthresholds: {deg_min_laps: 5}\n"
    )
    monkeypatch.setenv("HOME", str(home))
    store = ConfigStore()
    store.set_track(7)
    s = store.current()
    assert s.track is not None
    # user overlay wins over packaged, packaged keys still present (deep merge)
    assert s.track.pit_loss_s["green"] == 19.0
    assert s.track.pit_loss_s["vsc"] == 12.0
    assert s.track.name == "silverstone"
    # overlay threshold merged into settings.thresholds
    assert s.thresholds["deg_min_laps"] == 5


def test_missing_track_overlay_gives_none() -> None:
    store = ConfigStore()
    store.set_track(7)
    assert store.current().track is not None
    store.set_track(99)  # no overlay anywhere
    assert store.current().track is None


def test_set_override_nested() -> None:
    store = ConfigStore()
    assert store.set_override(("mindset", "active"), "aggressive") is True
    assert store.current().mindset.active == "aggressive"
