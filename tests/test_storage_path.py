from pathlib import Path

from pitwall.cli.common import _resolve_recording
from pitwall.cli.parser import build_parser
from pitwall.config.loader import config_hash
from pitwall.config.models import Settings
from pitwall.config.paths import HOME_ENV, storage_path


def test_relative_without_home(monkeypatch):
    monkeypatch.delenv(HOME_ENV, raising=False)
    assert storage_path("recordings") == Path("recordings")


def test_relative_anchored_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    assert storage_path("recordings") == tmp_path / "recordings"


def test_absolute_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    absolute = tmp_path / "elsewhere" / "x"
    assert storage_path(str(absolute)) == absolute


def test_tilde_expands_not_anchored(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    assert storage_path("~/x") == Path("~/x").expanduser()
    assert storage_path("~/x").is_absolute()


def test_resolve_recording_caller_relative(tmp_path, monkeypatch):
    caller = tmp_path / "caller"
    caller.mkdir()
    race = caller / "race.f1bin"
    race.touch()
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    monkeypatch.chdir(caller)
    assert _resolve_recording("./race.f1bin", Settings()).resolve() == race.resolve()


def test_resolve_recording_bare_name_uses_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    rec_dir = home / "recordings"
    rec_dir.mkdir(parents=True)
    (rec_dir / "foo.f1bin").touch()
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.chdir(caller)
    assert _resolve_recording("foo", Settings()) == rec_dir / "foo.f1bin"


def test_config_hash_ignores_home(tmp_path, monkeypatch):
    monkeypatch.delenv(HOME_ENV, raising=False)
    without = config_hash(Settings())
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    assert config_hash(Settings()) == without


def _record_scenarios_dir(monkeypatch):
    seen: list[Path] = []

    def fake_load_scenarios(directory, only=None):
        seen.append(directory)
        raise ValueError("stop")

    monkeypatch.setattr("pitwall.learning.bench.load_scenarios", fake_load_scenarios)
    return seen


def test_bench_scenarios_default_uses_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    caller = tmp_path / "caller"
    caller.mkdir()
    seen = _record_scenarios_dir(monkeypatch)
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.chdir(caller)
    args = build_parser().parse_args(["bench"])
    assert args.func(args) == 1
    assert seen[0] == home / "scenarios"


def test_bench_scenarios_flag_stays_caller_relative(tmp_path, monkeypatch):
    home = tmp_path / "home"
    caller = tmp_path / "caller"
    caller.mkdir()
    seen = _record_scenarios_dir(monkeypatch)
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.chdir(caller)
    args = build_parser().parse_args(["bench", "--scenarios", "mine"])
    assert args.func(args) == 1
    assert seen[0] == Path("mine")
