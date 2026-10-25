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
from pitwall.config.loader import profile_path


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
    if "https_cert" in connection or "https_key" in connection:
        return False
    lines = text.splitlines(keepends=True)
    matches = [
        i
        for i, line in enumerate(lines)
        if re.fullmatch(r"connection:\s*(#.*)?", line.rstrip("\n"))
    ]
    if len(matches) != 1:
        return False
    indent = "  "
    for line in lines[matches[0] + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace():
            indent = line[: len(line) - len(line.lstrip())]
        break
    lines[matches[0] + 1 : matches[0] + 1] = [
        f"{indent}https_cert: {json.dumps(cert)}\n",
        f"{indent}https_key: {json.dumps(key)}\n",
    ]
    profile.write_text("".join(lines), encoding="utf-8")
    return True


def cmd_cert(args: argparse.Namespace) -> int:
    """Make a trusted LAN certificate with mkcert and enable HTTPS."""
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
    profile = profile_path() or Path.home() / ".pitwall" / "profile.yaml"
    cert_s, key_s = cert.as_posix(), key.as_posix()
    if not set_profile_cert(profile, cert_s, key_s):
        print(f"cert: set these under connection: in {profile}, then restart Pitwall:")
        print(f'  https_cert: "{cert_s}"')
        print(f'  https_key: "{key_s}"')
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
    print("If this computer's LAN address changes, run pitwall cert again.")
    print(
        f'To turn HTTPS off, set connection.https_cert and connection.https_key to "" in {profile}.'
    )
    return 0
