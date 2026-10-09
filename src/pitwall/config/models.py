"""Settings model (pydantic v2). Layered: packaged defaults -> profile -> live
overrides, deep-merged, hash-stamped (docs/08)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from pitwall.net.profile import ProfileName


class ConnectionSettings(BaseModel):
    udp_host: str = "0.0.0.0"
    udp_port: int = 20777
    send_rate_hz: int = 30
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    require_pin: bool = True
    pin_trust_localhost: bool = True
    https_cert: str = ""
    https_key: str = ""
    shutdown_timeout_s: int = 3


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
    watchdog: bool = True  # `pitwall start` = recorder/supervisor + engine child
    engine_port: int = 20787  # loopback port the supervisor forwards datagrams to
    watchdog_stall_s: float = 20.0
    watchdog_grace_s: float = 60.0
    watchdog_backoff_max_s: float = 10.0
    watchdog_reset_s: float = 60.0
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
    min_gap_defer_s: float = 10.0  # a call inside min_gap waits up to this long, else dropped
    dedupe_window_s: float = 20.0
    quiet: bool = False
    mute_until_lap: int = 0
    p3_straight_only: bool = True
    p3_straight_wait_s: float = 8.0  # extra deadline while a P3 call waits for a straight


class UiSettings(BaseModel):
    state_hz: int = 5
    pages: list[str] = Field(default_factory=lambda: ["race", "car", "track"])
    auto_page: bool = False  # contextual page switching (race / track); driver press wins
    auto_page_manual_hold_s: float = 60.0  # no auto switch this long after a manual choice
    auto_page_battle_gap_s: float = 1.0  # rival ahead/behind inside this = battle page
    auto_page_call_hold_s: float = 8.0  # no auto switch this soon after a call went out


class ShortcutBinding(BaseModel):
    """A dedicated button that asks one menu item directly (docs/12)."""

    bit: int
    item: str  # menu item id


class MenuOpenActions(BaseModel):
    """What the page / mindset buttons do while the menu is open ("" = as usual)."""

    page: Literal["confirm", "close", ""] = ""
    mindset: Literal["confirm", "close", ""] = ""


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
    bookmark_replies: list[str] = Field(default_factory=lambda: ["Marked."])
    quiet_minutes: float = 5.0
    udp_action_bit: int = 0x00100000
    long_press: Literal["bookmark", "silent"] = "bookmark"
    silent_toggle_bit: int = 0  # an unused UDP Action bit toggles radio silence
    silent_keeps_p1: bool = True
    mindset_toggle_bit: int = 0  # e.g. 0x01000000 = UDP Action 5; balanced <-> aggressive
    mindset_cycle: list[str] = Field(default_factory=lambda: ["balanced", "aggressive"])
    mindset_replies: dict[str, str] = Field(
        default_factory=lambda: {
            "balanced": "Copy, balanced.",
            "aggressive": "Copy, aggressive. Pushing.",
        }
    )
    page_cycle_bit: int = 0  # e.g. 0x00800000 = UDP Action 4; next dashboard page
    # Driver menu (docs/12): up/down open and scroll; Action 1 confirms while open.
    menu_up_bit: int = 0  # e.g. 0x00200000 = UDP Action 2
    menu_down_bit: int = 0  # e.g. 0x00400000 = UDP Action 3
    menu_close_bit: int = 0  # dedicated close button; closes without answering
    menu_open_actions: MenuOpenActions = Field(default_factory=MenuOpenActions)
    shortcuts: list[ShortcutBinding] = Field(default_factory=list)
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
            "Radio's back on.",
            "Back on the radio.",
        ]
    )
    negative_mute_laps: int = 3

    @model_validator(mode="after")
    def _unique_bits(self) -> InputSettings:
        bits = {
            "udp_action_bit": self.udp_action_bit,
            "silent_toggle_bit": self.silent_toggle_bit,
            "mindset_toggle_bit": self.mindset_toggle_bit,
            "page_cycle_bit": self.page_cycle_bit,
            "menu_up_bit": self.menu_up_bit,
            "menu_down_bit": self.menu_down_bit,
            "menu_close_bit": self.menu_close_bit,
        }
        items: set[str] = set()
        for i, sc in enumerate(self.shortcuts):
            if sc.item in items:
                raise ValueError(f"input.shortcuts: item {sc.item!r} bound twice")
            items.add(sc.item)
            bits[f"shortcuts[{i}]"] = sc.bit
        seen: dict[int, str] = {}
        for name, bit in bits.items():
            if not bit:
                continue
            if bit in seen:
                raise ValueError(f"input.{name} and input.{seen[bit]} share bit {bit:#010x}")
            seen[bit] = name
        return self


MenuAction = Literal["mindset", "silent", "page", "budget"]


class MenuItemModel(BaseModel):
    """One driver-menu entry (docs/12). `kind`:
    question -> answered from the snapshot by the `answer` handler (defaults to id);
    opinion  -> recorded (decision log + SQLite) and acknowledged from `replies`;
    action   -> runs `action` (mindset / silent / page / budget)."""

    id: str
    label: str  # shown on the overlay and spoken on scroll; keep it 2-3 words
    kind: Literal["question", "opinion", "action"] = "question"
    answer: str = ""
    action: MenuAction | None = None
    topic: str = ""  # opinions: items sharing a topic replace each other (e.g. "balance")
    # Rule-style expressions on the snapshot at menu open: hide unless
    # `show_when`, float to the top while `rank_when` (YAML order otherwise).
    show_when: str = ""
    rank_when: str = ""
    shortcut_only: bool = False
    related_rules: list[str] = Field(default_factory=list)
    # case -> reply templates (variants rotate). Opinions and actions use "default".
    replies: dict[str, list[str]] = Field(default_factory=dict)


class MenuSettings(BaseModel):
    enabled: bool = True
    timeout_s: float = 6.0  # idle seconds before the menu closes by itself
    speak_on_scroll: bool = True  # speak each item name as it is highlighted
    wrap: bool = True
    opinion_hold_laps: int = 5  # a balance opinion biases advice this many laps
    # "budget" action cycles the P2/P3 calls-per-lap limit through these (overrides mindset)
    budget_steps: list[int] = Field(default_factory=lambda: [4, 8, 12, 20])
    items: list[MenuItemModel] = Field(default_factory=list)


class VoiceSettings(BaseModel):
    """Listen while the driver menu is open or after V; Action 1 keeps doc-12 gestures."""

    enabled: bool = False
    engine: Literal["sapi"] = "sapi"
    recognizer: str = ""  # SAPI recogniser token description substring; "" = first installed
    device: int = 0  # index into the SAPI audio inputs (`pitwall voice devices`)
    early_close_ms: int = 300  # SAPI CompleteResponseSpeed: silence after a full phrase
    close_silence_ms: int = 1000  # SAPI IncompleteResponseSpeed: silence after a partial one
    max_open_s: float = 6.0  # hard cap on an open channel
    key_max_open_s: float = 2.0  # cap for a V key open
    open_warn_s: float = 3.0  # dashboard turns amber after this with nothing recognised
    confidence_min: float = 0.7  # EngineConfidence below this is a miss
    priority: Literal["below_normal", "normal"] = "below_normal"
    affinity_mask: int = 0  # 0 = leave the OS default
    intents: dict[str, list[str]] = Field(default_factory=dict)  # intent -> phrases


class PersistenceSettings(BaseModel):
    enabled: bool = True
    path: str = "~/.pitwall/pitwall.sqlite"


class LearningSettings(BaseModel):
    auto_calibrate: bool = True  # maintain() refits priors and tunes cooldowns
    pack_dir: str = "learnings"
    pack_keep_days: int = 30


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
    tone_urgent_speed: float = 1.1
    tone_urgent_expression: float = 1.2
    tone_calm_speed: float = 0.95
    tone_calm_expression: float = 0.85


class MindsetSettings(BaseModel):
    active: str = "balanced"


class EscalationModel(BaseModel):
    """Phrases used once a call has triggered `after` times inside the rule's repeat window."""

    after: int = Field(ge=2)
    say: list[str]


class SeverityModel(BaseModel):
    """Phrases (and optionally a more urgent priority) used while `when` holds;
    the first matching tier wins over the base `say` pool."""

    when: str
    say: list[str]
    priority: Literal[1, 2, 3] | None = None


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
    conflict_group: str = ""  # queued calls in one group: only the one still true is kept
    supersedes: list[str] = Field(default_factory=list)  # rule ids this call replaces in the queue
    max_per_stint: int | None = None
    min_lap: int = 0
    requires: list[str] = Field(default_factory=list)
    say: str | list[str] = ""
    escalate: list[EscalationModel] = Field(default_factory=list)
    severity: list[SeverityModel] = Field(default_factory=list)
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
    thresholds: dict[str, float | dict[int, float]] = Field(default_factory=dict)


class Settings(BaseModel):
    connection: ConnectionSettings = Field(default_factory=ConnectionSettings)
    recording: RecordingSettings = Field(default_factory=RecordingSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    input: InputSettings = Field(default_factory=InputSettings)
    menu: MenuSettings = Field(default_factory=MenuSettings)
    persistence: PersistenceSettings = Field(default_factory=PersistenceSettings)
    learning: LearningSettings = Field(default_factory=LearningSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    mindset: MindsetSettings = Field(default_factory=MindsetSettings)
    thresholds: dict[str, float | dict[int, int] | dict[int, float]] = Field(default_factory=dict)
    setup_rules: dict[str, Any] = Field(default_factory=dict)
    track: TrackOverlay | None = None
    mindsets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rules: list[RuleDefModel] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_supersedes(self) -> Settings:
        rule_ids = {rule.id for rule in self.rules}
        for rule in self.rules:
            for superseded_id in rule.supersedes:
                if superseded_id not in rule_ids:
                    raise ValueError(f"rule {rule.id!r} supersedes unknown rule {superseded_id!r}")
        return self

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
