"""Layered config loader: packaged defaults -> profile -> live overrides,
deep-merged, validated, and hash-stamped. ConfigStore polls mtimes and keeps
the last good config on invalid input (docs/08)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from pitwall.config.models import Settings

DEFAULTS_DIR = Path(__file__).parent / "defaults"


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml_dir(path: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if not path.is_dir():
        return merged
    for f in sorted(path.rglob("*.yaml"), key=lambda p: (p.stem != "shared", str(p))):
        data = yaml.safe_load(f.read_text()) or {}
        rules = data.get("rules")
        if rules and merged.get("rules"):
            merged["rules"] = merged["rules"] + rules
            data = {k: v for k, v in data.items() if k != "rules"}
        merged = _deep_merge(merged, data)
    return merged


def profile_path() -> Path | None:
    env = os.environ.get("PITWALL_PROFILE")
    if env:
        return Path(env)
    default = Path.home() / ".pitwall" / "profile.yaml"
    return default if default.exists() else None


def config_hash(settings: Settings) -> str:
    canonical = json.dumps(settings.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


class ConfigStore:
    """current() -> (Settings, hash); reload() re-reads sources; invalid input
    keeps the last good config and records last_error."""

    def __init__(
        self,
        overrides: dict[str, Any] | None = None,
        rules_dir: Path | None = None,
    ) -> None:
        self._overrides = overrides or {}
        self._rules_dir = rules_dir
        self._profile = profile_path()
        self._mtimes: dict[Path, float] = {}
        self._last_poll = 0.0
        self.last_error: str | None = None
        self._settings: Settings | None = None
        self.reload()

    def _sources(self) -> list[Path]:
        srcs = sorted(DEFAULTS_DIR.rglob("*.yaml"))
        if self._rules_dir is not None and self._rules_dir.is_dir():
            srcs += sorted(self._rules_dir.rglob("*.yaml"))
        if self._profile is not None:
            srcs.append(self._profile)
        return srcs

    def _build(self) -> Settings:
        merged = _load_yaml_dir(DEFAULTS_DIR)
        if self._rules_dir is not None:
            # A rules directory REPLACES the packaged rules; any other keys in
            # it (thresholds, engine, ...) deep-merge on top.
            custom = _load_yaml_dir(self._rules_dir)
            custom_rules = custom.pop("rules", None)
            merged = _deep_merge(merged, custom)
            if custom_rules is not None:
                merged["rules"] = custom_rules
        if self._profile is not None and self._profile.exists():
            merged = _deep_merge(merged, yaml.safe_load(self._profile.read_text()) or {})
        merged = _deep_merge(merged, self._overrides)
        return Settings.model_validate(merged)

    def reload(self) -> bool:
        """Re-read all sources. Returns True if the config changed."""
        try:
            settings = self._build()
        except (ValidationError, yaml.YAMLError, OSError) as e:
            self.last_error = str(e)
            return False
        self.last_error = None
        changed = self._settings is None or settings != self._settings
        self._settings = settings
        for p in self._sources():
            try:
                self._mtimes[p] = p.stat().st_mtime
            except OSError:
                pass
        return changed

    def poll(self, now: float | None = None) -> bool:
        """Check source mtimes (at most 1/s) and reload if any changed."""
        now = time.monotonic() if now is None else now
        if now - self._last_poll < 1.0:
            return False
        self._last_poll = now
        for p in self._sources():
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if self._mtimes.get(p) != mtime:
                return self.reload()
        return False

    def current(self) -> Settings:
        assert self._settings is not None
        return self._settings

    @property
    def hash(self) -> str:
        return config_hash(self.current())
