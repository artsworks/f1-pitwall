"""`pitwall cert`: trusted HTTPS for the phone radio page via mkcert."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from pitwall.cli.serve import _lan_ip
from pitwall.config.loader import ConfigStore, profile_path


def set_profile_cert(profile: Path, cert: str, key: str) -> bool:
    """Write connection.https_cert/https_key into the profile, editing the YAML
    textually so user comments survive. Returns True when the profile now holds
    these values, False when the user must edit it by hand (file untouched)."""
    text = profile.read_text(encoding="utf-8") if profile.exists() else ""
    data = yaml.safe_load(text)
    if data is not None and not isinstance(data, dict):
        return False
    if data is None or "connection" not in data:
        profile.parent.mkdir(parents=True, exist_ok=True)
        sep = "" if not text or text.endswith("\n") else "\n"
        profile.write_text(
            text
            + sep
            + f"connection:\n  https_cert: {json.dumps(cert)}\n  https_key: {json.dumps(key)}\n",
            encoding="utf-8",
        )
        return True
    connection = data["connection"]
    if not isinstance(connection, dict):
        return False
    if connection.get("https_cert") == cert and connection.get("https_key") == key:
        return True
    wanted = {"https_cert": cert, "https_key": key}
    if any(
        field in connection and connection[field] not in ("", None, value)
        for field, value in wanted.items()
    ):
        return False
    lines = text.splitlines(keepends=True)
    matches = [
        i
        for i, line in enumerate(lines)
        if re.fullmatch(r"connection:\s*(#.*)?", line.rstrip("\n"))
    ]
    if len(matches) != 1:
        return False
    conn_line = matches[0]
    for field, value in wanted.items():
        if field not in connection:
            continue
        hits = [
            i
            for i, line in enumerate(lines[conn_line + 1 :], start=conn_line + 1)
            if re.match(rf"\s+{field}:", line)
        ]
        if len(hits) != 1:
            return False
        hit = hits[0]
        indent = lines[hit][: len(lines[hit]) - len(lines[hit].lstrip())]
        match = re.match(
            rf"(\s*{field}:)(.*?)(\s+#.*)?$", lines[hit].rstrip("\n")
        )
        comment = ""
        if match and yaml.safe_load(match.group(2) or "") == connection[field]:
            comment = match.group(3) or ""
        lines[hit] = f"{indent}{field}: {json.dumps(value)}{comment}\n"
    missing = [(field, value) for field, value in wanted.items() if field not in connection]
    if missing:
        indent = "  "
        for line in lines[conn_line + 1 :]:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if line[:1].isspace():
                indent = line[: len(line) - len(line.lstrip())]
            break
        lines[conn_line + 1 : conn_line + 1] = [
            f"{indent}{field}: {json.dumps(value)}\n" for field, value in missing
        ]
    profile.write_text("".join(lines), encoding="utf-8")
    return True


def cmd_cert(args: argparse.Namespace) -> int:
    """Make a trusted LAN certificate with mkcert and enable HTTPS."""
    if args.status:
        connection = ConfigStore().current().connection
        print("https" if connection.https_cert and connection.https_key else "http")
        return 0
    mkcert = shutil.which("mkcert")
    if mkcert is None:
        print("cert: mkcert is not installed. Install it, then run pitwall cert again.")
        if sys.platform == "win32":
            print("  winget install --id FiloSottile.mkcert -e")
        elif sys.platform == "darwin":
            print("  brew install mkcert")
        else:
            print("  sudo apt install mkcert (or your package manager's mkcert package)")
        return 1
    ip = args.ip or _lan_ip()
    if args.ip is None and ip == "127.0.0.1":
        print("cert: could not find this computer's LAN address. Run pitwall cert --ip <address>.")
        return 1
    directory = Path.home() / ".pitwall"
    directory.mkdir(parents=True, exist_ok=True)
    cert = directory / "phone.pem"
    key = directory / "phone-key.pem"
    if subprocess.run([mkcert, "-install"], check=False).returncode != 0:
        print("cert: mkcert -install failed (see above).")
        return 1
    if (
        subprocess.run(
            [
                mkcert,
                "-cert-file",
                str(cert),
                "-key-file",
                str(key),
                ip,
                "localhost",
                "127.0.0.1",
            ],
            check=False,
        ).returncode
        != 0
    ):
        print("cert: mkcert could not make the certificate (see above).")
        return 1
    if sys.platform != "win32":
        directory.chmod(0o700)
        if key.exists():
            key.chmod(0o600)
    profile = profile_path() or Path.home() / ".pitwall" / "profile.yaml"
    cert_s, key_s = cert.as_posix(), key.as_posix()
    if not set_profile_cert(profile, cert_s, key_s):
        print(f"cert: set these under connection: in {profile}, then restart Pitwall:")
        print(f'  https_cert: "{cert_s}"')
        print(f'  https_key: "{key_s}"')
        return 1
    effective = ConfigStore().current().connection
    if effective.https_cert != cert_s or effective.https_key != key_s:
        print(
            f"cert: {profile} was updated, but the settings Pitwall loads still differ. "
            "Check connection.https_cert and https_key, then run pitwall cert again."
        )
        return 1
    caroot = (
        subprocess.run(
            [mkcert, "-CAROOT"], capture_output=True, encoding="utf-8", check=False
        ).stdout
        or ""
    ).strip()
    root_ca = Path(caroot) / "rootCA.pem"
    print(
        f"HTTPS is set up. Restart Pitwall. The dashboard is now at "
        f"https://{ip}:8000 and http:// no longer works."
    )
    print(
        f"This computer trusts the certificate. On each other device that opens the "
        f"dashboard, such as the phone or the game PC, install {root_ca} as a trusted "
        f"certificate. The mkcert README explains the phone steps: "
        f"https://github.com/FiloSottile/mkcert"
    )
    print(f"Phone radio: open https://{ip}:8000/radio")
    print(f"Keep {key} private. Do not copy or share it, or make it readable by others.")
    print("If this computer's LAN address changes, run pitwall cert again.")
    print(
        f'To turn HTTPS off, set connection.https_cert and connection.https_key to "" in {profile}.'
    )
    return 0
