"""Settings model (pydantic v2). Layered: packaged defaults -> profile -> live
overrides, deep-merged, hash-stamped (docs/08)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from pitwall.net.profile import ProfileName


class ConnectionSettings(BaseModel):
    udp_host: str = "0.0.0.0"
    udp_port: int = 20777
    send_rate_hz: int = 30
    http_host: str = "0.0.0.0"
    http_port: int = 8000


class RecordingSettings(BaseModel):
    enabled: bool = True
    directory: str = "recordings"
    profile: ProfileName = "lite"
    compress_on_close: bool = True


class EngineSettings(BaseModel):
    tick_hz: int = 10
    ema_fast_s: float = 3.0
    ema_slow_s: float = 30.0
    staleness_s: dict[str, float] = Field(
        default_factory=lambda: {
            "session": 2.0,
            "lap_data": 0.5,
            "car_telemetry": 0.5,
            "car_status": 0.5,
            "car_damage": 0.5,
        }
    )


class PolicySettings(BaseModel):
    verbosity: Literal["silent", "critical", "normal", "coach"] = "normal"
    deadlines_s: dict[int, float] = Field(default_factory=lambda: {1: 5.0, 2: 3.0, 3: 1.5})
    calls_per_lap: int = 4
    min_gap_s: float = 3.0
    dedupe_window_s: float = 20.0
    quiet: bool = False
    mute_until_lap: int = 0


class UiSettings(BaseModel):
    state_hz: int = 5


class SpeechSettings(BaseModel):
    enabled: bool = True
    engine: Literal["auto", "piper", "sapi", "null"] = "auto"
    voice: str | None = None
    piper_voice: str = "en_GB-alan-medium"
    voices_dir: str = "voices"
    rate: int = 0
    volume: int = 100
    radio_click: bool = True


class MindsetSettings(BaseModel):
    active: str = "balanced"


class RuleDefModel(BaseModel):
    """Validated rule definition (rules.RuleDef mirrors this at runtime)."""

    id: str
    sessions: list[str] = Field(default_factory=list)
    priority: Literal[1, 2, 3]
    when: str
    clear_when: str | None = None
    still_true: str | None = None
    cooldown_s: float = 0.0
    max_per_stint: int | None = None
    min_lap: int = 0
    requires: list[str] = Field(default_factory=list)
    say: str = ""
    screen_only: bool = False
    tags: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    connection: ConnectionSettings = Field(default_factory=ConnectionSettings)
    recording: RecordingSettings = Field(default_factory=RecordingSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    mindset: MindsetSettings = Field(default_factory=MindsetSettings)
    thresholds: dict[str, float] = Field(default_factory=dict)
    mindsets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rules: list[RuleDefModel] = Field(default_factory=list)

    def resolved_mindset(self) -> dict[str, Any]:
        """Active mindset vector with `inherits` resolution."""
        return resolve_mindset(self.mindsets, self.mindset.active)


def resolve_mindset(mindsets: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    entry = dict(mindsets.get(name, {}))
    parent = entry.pop("inherits", None)
    if parent:
        base = resolve_mindset(mindsets, str(parent))
        base.update(entry)
        return base
    return entry
