"""Setup advisor: shared recommendation pipeline and live-call state."""

from __future__ import annotations

import copy
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pitwall.setup.evaluate import Recommendation, evaluate, setup_modes
from pitwall.setup.learn import learned_gains
from pitwall.setup.rules import SetupRules, parse_setup_rules, reason_for_symptom
from pitwall.setup.signals import RunSignals, session_signals

if TYPE_CHECKING:
    from pitwall.config.models import Settings
    from pitwall.state.session import SessionState
    from pitwall.store.db import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionAdvice:
    signals: RunSignals
    setup: Mapping[str, float]
    rules: SetupRules
    learned: Mapping[tuple[str, str, int], tuple[float, float]]
    recommendations: tuple[Recommendation, ...]


def recommend_for_session(
    db: Database,
    uid: int,
    settings: Settings,
    mode: str | None,
    *,
    run_choice: Literal["latest", "longest"],
    parc_ferme: int,
    lap: int | None,
    setup: Mapping[str, float] | None = None,
    rules: SetupRules | None = None,
    track_id: int | None = None,
    store: bool = True,
) -> SessionAdvice | None:
    signals = session_signals(db, uid, settings.thresholds, run_choice=run_choice)
    if signals is None:
        return None
    modes = setup_modes(signals.session_type) if mode is None else (mode,)
    if setup is None:
        setup = (
            db.setup_state_fields(signals.setup_state_id)
            if signals.setup_state_id is not None
            else None
        ) or {}
    if rules is None:
        rules = parse_setup_rules(settings.setup_rules)
    learned = learned_gains(db, signals.track_id, signals.compound)
    recommendations = tuple(
        rec
        for m in modes
        for rec in evaluate(
            signals,
            setup,
            mode=m,
            parc_ferme=parc_ferme,
            rules=rules,
            thresholds=settings.thresholds,
            learned=learned,
        )
    )
    if store:
        for rec in recommendations:
            db.insert_setup_rec(
                rec,
                track_id=signals.track_id if track_id is None else track_id,
                compound=signals.compound,
                lap=signals.run_end_lap if lap is None else lap,
            )
    return SessionAdvice(
        signals=signals,
        setup=setup,
        rules=rules,
        learned=learned,
        recommendations=recommendations,
    )


class SetupAdvisor:
    def __init__(self, state: SessionState, setup_rules: Mapping[str, Any]) -> None:
        self.state = state
        self.rules = parse_setup_rules(setup_rules)
        self._rules_source = copy.deepcopy(setup_rules)
        self._rules_ref = setup_rules
        self.session_uid: int | None = None
        self.call_last_lap: dict[str, int] = {}
        self.call_param = ""
        self.call_from = 0.0
        self.call_to = 0.0
        self.call_reason = ""
        self.call_set_lap: int | None = None
        self.stop_wing_from = 0.0
        self.stop_wing_to = 0.0

    def refresh_rules(self, setup_rules: Mapping[str, Any]) -> None:
        if setup_rules is self._rules_ref:
            return
        self._rules_ref = setup_rules
        if setup_rules == self._rules_source:
            return
        self._rules_source = copy.deepcopy(setup_rules)
        try:
            self.rules = parse_setup_rules(setup_rules)
        except Exception:
            log.exception("setup rules reload failed; keeping the last valid rules")

    def reset(self, uid: int) -> None:
        self.session_uid = uid
        self.call_last_lap.clear()
        self._clear_call()
        self._clear_stop_wing()
        self.state.setup_advice = ()

    def clear(self) -> None:
        self.state.setup_advice = ()
        self._clear_call()
        self._clear_stop_wing()

    def _clear_call(self) -> None:
        self.call_param = ""
        self.call_from = 0.0
        self.call_to = 0.0
        self.call_reason = ""
        self.call_set_lap = None

    def _clear_stop_wing(self) -> None:
        self.stop_wing_from = 0.0
        self.stop_wing_to = 0.0

    def _live_setup_value(self, param: str) -> float:
        return (
            float(self.state.front_brake_bias)
            if param == "brake_bias"
            else float(self.state.setup_on_throttle_diff)
        )

    def _update_call(
        self, recommendations: tuple[Recommendation, ...], lap: int, cooldown_laps: int
    ) -> None:
        candidate = next(
            (
                rec
                for rec in sorted(
                    (item for item in recommendations if item.mode == "race"),
                    key=lambda item: item.tier != "primary",
                )
                if rec.param in {"brake_bias", "on_throttle"}
            ),
            None,
        )
        if candidate is None:
            self._clear_call()
            return

        param = candidate.param
        live_value = self._live_setup_value(param)
        if math.isclose(live_value, candidate.to_value, abs_tol=1e-6):
            self._clear_call()
            return

        if self.call_param:
            current_live = self._live_setup_value(self.call_param)
            if math.isclose(current_live, self.call_to, abs_tol=1e-6):
                self._clear_call()
            elif self.call_set_lap is not None and lap > self.call_set_lap:
                self._clear_call()
                return
            elif self.call_param == param:
                return
            else:
                self._clear_call()

        previous_lap = self.call_last_lap.get(param)
        if previous_lap is not None and lap - previous_lap < cooldown_laps:
            return
        self.call_param = param
        self.call_from = live_value
        self.call_to = candidate.to_value
        self.call_reason = reason_for_symptom(candidate.rule_id)
        self.call_set_lap = lap
        self.call_last_lap[param] = lap

    def update_stop_wing(self, stop_planned: bool) -> None:
        rec = next(
            (
                item
                for item in self.state.setup_advice
                if item.mode == "race_stop" and item.param == "front_wing"
            ),
            None,
        )
        if (
            rec is None
            or not stop_planned
            or math.isclose(self.state.next_front_wing_value, rec.to_value, abs_tol=1e-6)
        ):
            self._clear_stop_wing()
            return
        self.stop_wing_from = rec.from_value
        self.stop_wing_to = rec.to_value

    def evaluate_live(self, db: Any, uid: int, settings: Settings) -> None:
        thresholds = settings.thresholds
        try:
            advice = recommend_for_session(
                db,
                uid,
                settings,
                None,
                run_choice="latest",
                parc_ferme=self.state.parc_ferme_rules,
                lap=self.state.lap_num,
                setup=self.state.setup,
                rules=self.rules,
                track_id=self.state.track_id,
            )
            if advice is None:
                self.clear()
                return
            self.state.setup_advice = advice.recommendations
            if "race" in setup_modes(advice.signals.session_type):
                cooldown = thresholds.get("setup_rec_cooldown_laps", 5)
                cooldown_laps = int(cooldown) if isinstance(cooldown, int | float) else 5
                self._update_call(
                    advice.recommendations,
                    self.state.lap_num,
                    cooldown_laps,
                )
            else:
                self._clear_call()
                self._clear_stop_wing()
        except Exception:
            self.clear()
            log.exception("live setup advice failed; skipping this lap")

    def store_debrief(self, db: Any, uid: int, settings: Settings) -> None:
        try:
            advice = recommend_for_session(
                db,
                uid,
                settings,
                "debrief",
                run_choice="longest",
                parc_ferme=self.state.parc_ferme_rules,
                lap=None,
                rules=self.rules,
                track_id=self.state.track_id,
            )
            if advice is None:
                return
            self.state.setup_advice = (*self.state.setup_advice, *advice.recommendations)
        except Exception:
            log.exception("debrief setup advice failed; skipping")
