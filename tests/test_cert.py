import subprocess
from pathlib import Path

import yaml

from pitwall.cli.cert import set_profile_cert
from pitwall.cli.parser import build_parser
from pitwall.config.loader import ConfigStore


def test_set_profile_cert_creates_file(tmp_path):
    profile = tmp_path / "sub" / "profile.yaml"
    assert set_profile_cert(profile, "c.pem", "k.pem") is True
    data = yaml.safe_load(profile.read_text())
    assert data["connection"]["https_cert"] == "c.pem"
    assert data["connection"]["https_key"] == "k.pem"


def test_set_profile_cert_appends_block(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("# mine\nspeech:\n  rate: 2\n")
    assert set_profile_cert(profile, "c.pem", "k.pem") is True
    text = profile.read_text()
    assert text.startswith("# mine\nspeech:\n  rate: 2\n")
    data = yaml.safe_load(text)
    assert data["speech"]["rate"] == 2
    assert data["connection"]["https_cert"] == "c.pem"
    assert data["connection"]["https_key"] == "k.pem"


def test_set_profile_cert_keeps_comment_only_file(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("# only notes")
    assert set_profile_cert(profile, "c.pem", "k.pem") is True
    text = profile.read_text()
    assert text.startswith("# only notes\n")
    assert yaml.safe_load(text)["connection"]["https_cert"] == "c.pem"


def test_set_profile_cert_inserts_with_indent(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("connection:\n    http_port: 8001  # keep\n")
    assert set_profile_cert(profile, "c.pem", "k.pem") is True
    text = profile.read_text()
    assert "    http_port: 8001  # keep" in text
    data = yaml.safe_load(text)
    assert data["connection"]["http_port"] == 8001
    assert data["connection"]["https_cert"] == "c.pem"
    assert data["connection"]["https_key"] == "k.pem"


def test_set_profile_cert_replaces_empty_values(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text('connection:\n  https_cert: ""\n  https_key: ""\n')
    assert set_profile_cert(profile, "c.pem", "k.pem") is True
    data = yaml.safe_load(profile.read_text())
    assert data["connection"]["https_cert"] == "c.pem"
    assert data["connection"]["https_key"] == "k.pem"


def test_set_profile_cert_keeps_trailing_comments(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        'connection:\n  https_cert: ""  # phone cert\n  https_key: ~ # key\n'
    )
    assert set_profile_cert(profile, "c.pem", "k.pem") is True
    text = profile.read_text()
    assert '  https_cert: "c.pem"  # phone cert' in text
    assert '  https_key: "k.pem" # key' in text
    data = yaml.safe_load(text)
    assert data["connection"]["https_cert"] == "c.pem"
    assert data["connection"]["https_key"] == "k.pem"


def test_set_profile_cert_keeps_comment_after_single_quoted_empty_value(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("connection:\n  https_cert: ''  # note\n  https_key: ~\n")
    assert set_profile_cert(profile, "c.pem", "k.pem") is True
    text = profile.read_text()
    assert '  https_cert: "c.pem"  # note' in text
    data = yaml.safe_load(text)
    assert data["connection"]["https_cert"] == "c.pem"
    assert data["connection"]["https_key"] == "k.pem"


def test_set_profile_cert_replaces_empty_key_and_inserts_cert(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text('connection:\n  http_port: 8001\n  https_key: ""\n')
    assert set_profile_cert(profile, "c.pem", "k.pem") is True
    data = yaml.safe_load(profile.read_text())
    assert data["connection"]["http_port"] == 8001
    assert data["connection"]["https_cert"] == "c.pem"
    assert data["connection"]["https_key"] == "k.pem"


def test_set_profile_cert_existing_value_returns_false(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text('connection:\n  https_cert: "/mine.pem"\n')
    before = profile.read_bytes()
    assert set_profile_cert(profile, "c.pem", "k.pem") is False
    assert profile.read_bytes() == before


def test_set_profile_cert_flow_mapping_returns_false(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text('connection: {https_cert: "", https_key: ""}\n')
    before = profile.read_bytes()
    assert set_profile_cert(profile, "c.pem", "k.pem") is False
    assert profile.read_bytes() == before


def test_set_profile_cert_same_values_true(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text('connection:\n  https_cert: "c.pem"\n  https_key: "k.pem"\n')
    before = profile.read_bytes()
    assert set_profile_cert(profile, "c.pem", "k.pem") is True
    assert profile.read_bytes() == before


def test_cmd_cert_no_mkcert(monkeypatch, capsys):
    monkeypatch.delenv("PITWALL_PROFILE", raising=False)
    monkeypatch.setattr("pitwall.cli.cert.shutil.which", lambda name: None)
    args = build_parser().parse_args(["cert"])
    assert args.func(args) == 1
    assert "mkcert is not installed" in capsys.readouterr().out


def test_cmd_cert_no_lan(monkeypatch):
    monkeypatch.delenv("PITWALL_PROFILE", raising=False)
    monkeypatch.setattr("pitwall.cli.cert.shutil.which", lambda name: "mkcert")
    monkeypatch.setattr("pitwall.cli.cert._lan_ip", lambda: "127.0.0.1")
    args = build_parser().parse_args(["cert"])
    assert args.func(args) == 1


def test_cmd_cert_happy_path(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PITWALL_PROFILE", raising=False)
    monkeypatch.setattr("pitwall.cli.cert.shutil.which", lambda name: "mkcert")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=str(tmp_path / "ca"))

    monkeypatch.setattr("pitwall.cli.cert.subprocess.run", fake_run)
    args = build_parser().parse_args(["cert", "--ip", "192.168.1.20"])
    assert args.func(args) == 0
    assert calls[0] == ["mkcert", "-install"]
    assert "-cert-file" in calls[1]
    assert "192.168.1.20" in calls[1]
    out = capsys.readouterr().out
    assert "https://192.168.1.20:8000/radio" in out
    assert "rootCA.pem" in out
    assert (
        ConfigStore().current().connection.https_cert
        == (Path.home() / ".pitwall" / "phone.pem").as_posix()
    )


def test_cmd_cert_manual_edit_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PITWALL_PROFILE", raising=False)
    monkeypatch.setattr("pitwall.cli.cert.shutil.which", lambda name: "mkcert")
    profile = Path.home() / ".pitwall" / "profile.yaml"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text('connection:\n  https_cert: "/mine.pem"\n  https_key: "/mine-key.pem"\n')

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=str(tmp_path / "ca"))

    monkeypatch.setattr("pitwall.cli.cert.subprocess.run", fake_run)
    args = build_parser().parse_args(["cert", "--ip", "192.168.1.20"])
    assert args.func(args) == 1
    out = capsys.readouterr().out
    assert "set these under connection:" in out
    assert "HTTPS is set up" not in out


def test_cmd_cert_status_http(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PITWALL_PROFILE", raising=False)
    monkeypatch.setattr("pitwall.cli.cert.shutil.which", lambda name: None)
    args = build_parser().parse_args(["cert", "--status"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "http"


def test_cmd_cert_status_https(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PITWALL_PROFILE", raising=False)
    monkeypatch.setattr("pitwall.cli.cert.shutil.which", lambda name: None)
    profile = Path.home() / ".pitwall" / "profile.yaml"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text('connection:\n  https_cert: "c.pem"\n  https_key: "k.pem"\n')
    args = build_parser().parse_args(["cert", "--status"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "https"
