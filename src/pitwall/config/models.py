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
    straight_hold_s: float = 1.0  # full-throttle hold that counts as "on a straight"
    heartbeat_s: float = 5.0
    recovery_max_age_s: float = 300.0
    recovery_tail_s: float = 120.0
    staleness_s: dict[str, float] = Field(
        default_factory=lambda: {
            "session": 2.0,
            "lap_data": 0.5,
            "car_telemetry": 0.5,
            "car_status": 0.5,
            "car_damage": 0.5,
            "motion_ex": 0.5,
            "participants": 15.0,
            "car_setups": 5.0,
            "session_history": 5.0,
            "tyre_sets": 5.0,
            "car_telemetry_2": 1.0,
        }
    )


class PolicySettings(BaseModel):
    verbosity: Literal["silent", "critical", "normal", "coach"] = "normal"
    deadlines_s: dict[int, float] = Field(default_factory=lambda: {1: 5.0, 2: 3.0, 3: 1.5})
    calls_per_lap: int | None = None  # None -> verbosity preset table
    min_gap_s: float = 3.0
    dedupe_window_s: float = 20.0
    quiet: bool = False
    mute_until_lap: int = 0
    p3_straight_only: bool = True


class UiSettings(BaseModel):
    state_hz: int = 5
    pages: list[str] = Field(default_factory=lambda: ["race", "battle", "car", "track", "setup"])
    auto_page: bool = False  # contextual page switching (battle / track); driver press wins
    auto_page_manual_hold_s: float = 60.0  # no auto switch this long after a manual choice
    auto_page_battle_gap_s: float = 1.0  # rival ahead/behind inside this = battle page
    auto_page_call_hold_s: float = 8.0  # no auto switch this soon after a call went out


class InputSettings(BaseModel):
    double_press_ms: int = 350
    long_press_ms: int = 800
    bounce_ms: int = 60
    response_window_s: float = 8.0
    say_again: bool = True  # late single press re-speaks the last call
    say_again_window_s: float = 30.0
    spoken_replies: bool = False  # packaged settings.yaml turns this on
    ack_replies: list[str] = Field(default_factory=lambda: ["Copy.", "Copy that.", "Understood."])
    neg_replies: list[str] = Field(default_factory=lambda: ["Noted.", "Copy, noted."])
    quiet_minutes: float = 5.0
    udp_action_bit: int = 0x00100000
    long_press: Literal["bookmark", "silent"] = "bookmark"
    silent_toggle_bit: int = 0  # e.g. 0x00400000 = UDP Action 3; any press toggles
    silent_keeps_p1: bool = True
    mindset_toggle_bit: int = 0  # e.g. 0x00200000 = UDP Action 2; balanced <-> aggressive
    mindset_cycle: list[str] = Field(default_factory=lambda: ["balanced", "aggressive"])
    mindset_replies: dict[str, str] = Field(
        default_factory=lambda: {
            "balanced": "Copy, balanced.",
            "aggressive": "Copy, aggressive. Pushing.",
        }
    )
    page_cycle_bit: int = 0  # e.g. 0x00800000 = UDP Action 4; next dashboard page
    silent_on_replies: list[str] = Field(
        default_factory=lambda: [
            "Radio silent. Leave you to it.",
            "Copy, I'll leave you to it.",
            "Going silent. It's all yours.",
        ]
    )
    silent_off_replies: list[str] = Field(
        default_factory=lambda: [
            "Back with you. Feeding you info again.",
            "Radio's back on. I'll keep you posted.",
            "Back on the radio.",
        ]
    )
    negative_mute_laps: int = 3


class PersistenceSettings(BaseModel):
    enabled: bool = True
    path: str = "~/.pitwall/pitwall.sqlite"


class SpeechSettings(BaseModel):
    enabled: bool = True
    engine: Literal["auto", "piper", "sapi", "null"] = "auto"
    voice: str | None = None
    piper_voice: str = "en_GB-northern_english_male-medium"
    piper_speed: float = 1.3
    voices_dir: str = "voices"
    rate: int = 0
    volume: int = 100
    radio_click: bool = True


class MindsetSettings(BaseModel):
    active: str = "balanced"


class EscalationModel(BaseModel):
    """Phrases used once a call has triggered `after` times inside the rule's repeat window."""

    after: int = Field(ge=2)
    say: list[str]


class RuleDefModel(BaseModel):
    """Validated rule definition (rules.RuleDef mirrors this at runtime)."""

    id: str
    sessions: list[str] = Field(default_factory=list)
    priority: Literal[1, 2, 3]
    when: str
    clear_when: str | None = None
    still_true: str | None = None
    cooldown_s: float = 0.0
    cooldown_group: str = ""  # rules sharing a group share one cooldown clock
    max_per_stint: int | None = None
    min_lap: int = 0
    requires: list[str] = Field(default_factory=list)
    say: str | list[str] = ""
    escalate: list[EscalationModel] = Field(default_factory=list)
    repeat_window_s: float = 600.0
    screen_only: bool = False
    tags: list[str] = Field(default_factory=list)
    # Spoken reply when the driver acks / negs this call; generic replies if empty.
    on_ack: str | list[str] = ""
    on_neg: str | list[str] = ""
    response_window_s: float | None = None  # overrides input.response_window_s

    def say_pool(self) -> list[str]:
        if isinstance(self.say, str):
            return [self.say] if self.say else []
        return list(self.say)


class TrackOverlay(BaseModel):
    """Per-track overlay (docs/18): cold-start priors + threshold overrides.
    Packaged at config/defaults/tracks/<id>.yaml; ~/.pitwall/tracks wins."""

    track_id: int
    name: str = ""
    pit_loss_s: dict[str, float] = Field(default_factory=dict)  # green/vsc/sc
    pit_exit_m: float = 0.0
    pit_entry_m: float = 0.0
    fuel_kg_per_lap: float = 0.0
    deg_ms_per_lap: dict[int, float] = Field(default_factory=dict)  # actual compound -> ms
    base_pace_ms: int = 0  # 0 = unknown
    thresholds: dict[str, float] = Field(default_factory=dict)


class Settings(BaseModel):
    connection: ConnectionSettings = Field(default_factory=ConnectionSettings)
    recording: RecordingSettings = Field(default_factory=RecordingSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    input: InputSettings = Field(default_factory=InputSettings)
    persistence: PersistenceSettings = Field(default_factory=PersistenceSettings)
    mindset: MindsetSettings = Field(default_factory=MindsetSettings)
    thresholds: dict[str, float | dict[int, int]] = Field(default_factory=dict)
    track: TrackOverlay | None = None
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
