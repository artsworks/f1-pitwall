"""Layered config loader: packaged defaults -> profile -> live overrides,
deep-merged, validated, and hash-stamped. ConfigStore polls mtimes and keeps
the last good config on invalid input (docs/08)."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from pitwall.config.models import InputSettings, Settings

DEFAULTS_DIR = Path(__file__).parent / "defaults"

log = logging.getLogger(__name__)

# Must stay a safe loader (CSafeLoader or SafeLoader). Never switch to
# yaml.Loader or yaml.CLoader: config comes from user-editable profile and
# overlay paths, and full-loader deserialization can run arbitrary code.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_YAML_LOADER)


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml_dir(path: Path, *, exclude_dir: Path | None = None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if not path.is_dir():
        return merged
    files = (f for f in path.rglob("*.yaml") if exclude_dir is None or exclude_dir not in f.parents)
    for f in sorted(files, key=lambda p: (p.stem != "shared", str(p))):
        data = _load_yaml(f.read_text(encoding="utf-8")) or {}
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


def _yield_volume_bit(merged: dict[str, Any], profile: dict[str, Any]) -> None:
    """A profile that binds the default volume bit elsewhere, without setting
    input.volume_cycle_bit itself, keeps its binding and turns volume cycle off."""
    prof_in = profile.get("input")
    merged_in = merged.get("input")
    if not isinstance(prof_in, dict) or not isinstance(merged_in, dict):
        return
    bit = merged_in.get("volume_cycle_bit")
    if not bit or "volume_cycle_bit" in prof_in:
        return
    owned = [
        k
        for k, v in prof_in.items()
        if k.endswith("_bit") and k in InputSettings.model_fields and v == bit
    ]
    shortcuts = prof_in.get("shortcuts")
    if isinstance(shortcuts, list):
        owned += [
            f"shortcuts[{i}]"
            for i, sc in enumerate(shortcuts)
            if isinstance(sc, dict) and sc.get("bit") == bit
        ]
    if owned:
        merged_in["volume_cycle_bit"] = 0
        log.warning("profile binds input.%s to %#010x; volume cycle disabled", owned[0], bit)


class ConfigStore:
    """current() -> (Settings, hash); reload() re-reads sources; invalid input
    keeps the last good config and records last_error."""

    def __init__(
        self,
        overrides: dict[str, Any] | None = None,
        rules_dir: Path | None = None,
        isolated: bool = False,
    ) -> None:
        self._overrides = overrides or {}
        self._rules_dir = rules_dir
        self._isolated = isolated
        self._profile = None if isolated else profile_path()
        self._track_id: int | None = None
        self._mtimes: dict[Path, float | None] = {}
        self._last_poll = 0.0
        self.last_error: str | None = None
        self._settings: Settings | None = None
        self.reload()

    def _sources(self) -> list[Path]:
        srcs = sorted(DEFAULTS_DIR.rglob("*.yaml"))
        if self._rules_dir is not None and self._rules_dir.is_dir():
            srcs += sorted(self._rules_dir.rglob("*.yaml"))
        if not self._isolated and self._profile is not None:
            srcs.append(self._profile)
        return srcs

    def _watched_dirs(self) -> list[Path]:
        roots = [DEFAULTS_DIR]
        if self._rules_dir is not None:
            roots.append(self._rules_dir)
        dirs: list[Path] = []
        for root in roots:
            dirs.append(root)
            dirs += sorted(p for p in root.rglob("*") if p.is_dir())
        return dirs

    def _build(self) -> Settings:
        merged = _load_yaml_dir(DEFAULTS_DIR, exclude_dir=DEFAULTS_DIR / "tracks")
        if self._rules_dir is not None:
            # A rules directory REPLACES the packaged rules; any other keys in
            # it (thresholds, engine, ...) deep-merge on top.
            custom = _load_yaml_dir(self._rules_dir)
            custom_rules = custom.pop("rules", None)
            merged = _deep_merge(merged, custom)
            if custom_rules is not None:
                merged["rules"] = custom_rules
        if not self._isolated and self._profile is not None and self._profile.exists():
            profile = _load_yaml(self._profile.read_text(encoding="utf-8")) or {}
            merged = _deep_merge(merged, profile)
            _yield_volume_bit(merged, profile)
        overlay = None if self._isolated else self._load_track_overlay()
        if overlay is not None:
            overlay_thresholds = overlay.get("thresholds")
            th = dict(overlay_thresholds) if isinstance(overlay_thresholds, dict) else {}
            for name in ("pit_entry_m", "pit_exit_m"):
                if name in overlay and name not in th:
                    th[name] = overlay[name]
            if th:
                merged["thresholds"] = _deep_merge(merged.get("thresholds", {}), th)
            merged["track"] = overlay
        merged = _deep_merge(merged, self._overrides)
        return Settings.model_validate(merged)

    def _load_track_overlay(self) -> dict[str, Any] | None:
        """Packaged defaults/tracks/<id>.yaml deep-merged under
        ~/.pitwall/tracks/<id>.yaml (user wins). None when absent."""
        if self._track_id is None:
            return None
        overlay: dict[str, Any] = {}
        for path in (
            DEFAULTS_DIR / "tracks" / f"{self._track_id}.yaml",
            Path.home() / ".pitwall" / "tracks" / f"{self._track_id}.yaml",
        ):
            if path.is_file():
                overlay = _deep_merge(
                    overlay,
                    _load_yaml(path.read_text(encoding="utf-8")) or {},
                )
        if not overlay:
            return None
        overlay.setdefault("track_id", self._track_id)
        return overlay

    def set_track(self, track_id: int | None) -> bool:
        """Select the track overlay and reload. Returns True when settings changed."""
        if track_id == self._track_id:
            return False
        self._track_id = track_id
        return self.reload()

    def set_override(self, path: tuple[str, ...], value: object) -> bool:
        """Set a nested key in the live-overrides layer and reload.

        A value the reload rejects is rolled back, so later reloads still work.
        """
        previous = copy.deepcopy(self._overrides)
        node: dict[str, Any] = self._overrides
        for key in path[:-1]:
            nxt = node.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                node[key] = nxt
            node = nxt
        node[path[-1]] = value
        changed = self.reload()
        if self.last_error is not None:
            self._overrides = previous
            return False
        return changed

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
        watched = self._sources() + self._watched_dirs()
        mtimes: dict[Path, float | None] = {}
        for p in watched:
            try:
                mtimes[p] = p.stat().st_mtime
            except OSError:
                mtimes[p] = None
        self._mtimes = mtimes
        return changed

    def poll(self, now: float | None = None) -> bool:
        """Check source mtimes (at most 1/s) and reload if any changed."""
        now = time.monotonic() if now is None else now
        if now - self._last_poll < 1.0:
            return False
        self._last_poll = now
        for p, cached in self._mtimes.items():
            try:
                mtime: float | None = p.stat().st_mtime
            except OSError:
                mtime = None
            if mtime != cached:
                return self.reload()
        return False

    def current(self) -> Settings:
        assert self._settings is not None
        return self._settings

    @property
    def hash(self) -> str:
        return config_hash(self.current())
