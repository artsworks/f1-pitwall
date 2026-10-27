import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _home_is_tmp(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)


@pytest.fixture(autouse=True)
def _no_pitwall_home(monkeypatch):
    monkeypatch.delenv("PITWALL_HOME", raising=False)
