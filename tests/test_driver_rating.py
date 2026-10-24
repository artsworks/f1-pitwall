from __future__ import annotations

import pytest

from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.digest import DIGEST_VERSION, build_digest, format_digest
from pitwall.engine import build_engine
from pitwall.maintenance import quarantine_bad, rebuild_stint_params
from pitwall.model.driver import (
    DRIVER_COMPOUND,
    GLOBAL_TRACK_ID,
    PARAM_NAMES,
    RATING,
    DriverInputs,
    advice_scale,
    component_score,
    compute_profile,
    confidence,
    fold_weight,
    learned_rating,
)
from pitwall.protocol.header import PacketId
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database

from .synth import make_event_packet, pack_packet

TH = ConfigStore().current().thresholds


def inputs(**overrides: object) -> DriverInputs:
    values: dict[str, object] = {
        "lap_times_ms": (89_000, 90_000, 91_000),
        "field_lap_times_ms": (90_000, 91_000, 92_000),
        "base_ms": 90_500.0,
        "deg_ms_per_lap": 110.0,
        "deg_prior_ms_per_lap": 80.0,
        "laps": 10,
        "lockups": 2,
        "spins": 1,
        "offs": 1,
        "contacts": 2,
        "grid_position": 8,
        "finish_position": 5,
    }
    values.update(overrides)
    return DriverInputs(**values)  # type: ignore[arg-type]


def test_compute_profile_scores_available_components() -> None:
    profile = compute_profile(inputs(), TH)

    assert profile is not None
    assert profile.pace_delta_ms == -1_000
    assert profile.consistency_cv == pytest.approx(816.4965809 / 90_000)
    assert profile.deg_delta_ms_per_lap == 30
    assert profile.errors_per_lap == 0.4
    assert profile.contact_per_lap == 0.2
    assert profile.position_delta == 3
    scores = dict(profile.scores)
    assert scores["pace_delta_ms"] == pytest.approx(0.5 + 1_000 / 3_000)
    assert scores["consistency_cv"] == pytest.approx(1 - profile.consistency_cv / 0.02)
    assert scores["deg_delta_ms_per_lap"] == pytest.approx(0.25)
    assert scores["errors_per_lap"] == pytest.approx(0.2)
    assert scores["contact_per_lap"] == 0
    assert scores["position_delta"] == pytest.approx(0.65)
    assert profile.rating == pytest.approx(sum(scores.values()) / len(scores))


@pytest.mark.parametrize(
    ("component", "value", "expected"),
    [
        ("overtakes_per_lap", 1.0, 1.0),
        ("overtakes_per_lap", -1.0, 0.0),
        ("overtakes_per_lap", 0.0, 0.5),
        ("infringements_per_lap", 0.0, 1.0),
        ("infringements_per_lap", 0.5, 0.0),
    ],
)
def test_overtake_and_infringement_component_scores(
    component: str, value: float, expected: float
) -> None:
    assert component_score(component, value, TH) == expected


def test_compute_profile_uses_overtakes_and_weighted_infringements() -> None:
    profile = compute_profile(
        inputs(
            laps=8,
            overtakes_made=8,
            overtakes_lost=0,
            warnings=1,
            penalties=1,
        ),
        TH,
    )

    assert profile is not None
    assert profile.overtakes_per_lap == 1.0
    assert profile.infringements_per_lap == 0.5
    scores = dict(profile.scores)
    assert scores["overtakes_per_lap"] == 1.0
    assert scores["infringements_per_lap"] == 0.0
    assert profile.rating == pytest.approx(sum(scores.values()) / len(scores))


def test_compute_profile_uses_base_when_field_is_small() -> None:
    profile = compute_profile(inputs(field_lap_times_ms=(90_000, 91_000), base_ms=90_500.0), TH)

    assert profile is not None
    assert profile.pace_delta_ms == -500


def test_compute_profile_skips_missing_components() -> None:
    profile = compute_profile(
        inputs(
            field_lap_times_ms=(90_000, 91_000),
            base_ms=None,
            deg_ms_per_lap=None,
            deg_prior_ms_per_lap=None,
            grid_position=0,
            finish_position=0,
            lockups=0,
            spins=0,
            offs=0,
            contacts=0,
        ),
        TH,
    )

    assert profile is not None
    assert profile.pace_delta_ms is None
    assert profile.deg_delta_ms_per_lap is None
    assert profile.position_delta is None
    assert profile.overtakes_per_lap is None
    assert profile.infringements_per_lap is None
    assert {name for name, _ in profile.scores} == {
        "consistency_cv",
        "errors_per_lap",
        "contact_per_lap",
    }
    assert profile.rating == pytest.approx(sum(score for _, score in profile.scores) / 3)


@pytest.mark.parametrize("lap_times,laps", [((90_000, 91_000), 5), ((90_000, 91_000, 92_000), 0)])
def test_compute_profile_requires_minimum_data(lap_times: tuple[int, ...], laps: int) -> None:
    assert compute_profile(inputs(lap_times_ms=lap_times, laps=laps), TH) is None


def test_fold_weight_maps_session_types_and_accepts_overrides() -> None:
    assert [fold_weight(stype, TH) for stype in range(1, 5)] == [0, 0, 0, 0]
    assert [fold_weight(stype, TH) for stype in range(5, 15)] == [1] * 10
    assert [fold_weight(stype, TH) for stype in range(15, 18)] == [5, 5, 5]
    assert fold_weight(18, TH) == 0
    assert fold_weight(15, {"driver_fold_weight_race": 2.5}) == 2.5


def test_confidence_and_advice_scaling() -> None:
    assert confidence(5, TH) == pytest.approx(1 / 3)
    assert advice_scale(0.3, 0.5, TH).confidence_factor == pytest.approx(0.94)
    assert advice_scale(0.3, 0.5, TH).risk_add == pytest.approx(0.04)
    assert advice_scale(0.3, 0.5, TH).pace_cap_ms == pytest.approx(60)
    assert advice_scale(0.5, 1.0, TH).confidence_factor == 1.0
    assert advice_scale(0.8, 1.0, TH).risk_add == 0.0

    settings = ConfigStore().current()
    aggr = settings.mindsets["aggressive"]
    unknown = advice_scale(0.5, 0.0, TH, aggr, source="")
    assert unknown.pit_confidence_min == pytest.approx(0.70)
    assert unknown.position_loss_risk_max == pytest.approx(0.30)
    assert unknown.pass_prob_min == pytest.approx(0.30)

    low = advice_scale(0.3, 0.8, TH, aggr, source="track")
    assert low.pit_confidence_min == pytest.approx(0.70)
    assert low.position_loss_risk_max == pytest.approx(0.30)
    assert low.pass_prob_min == pytest.approx(0.30)

    mid = advice_scale(0.75, 0.8, TH, aggr, source="track")
    high = advice_scale(1.0, 1.0, TH, aggr, source="track")
    assert 0.55 < mid.pit_confidence_min < 0.70
    assert 0.30 < mid.position_loss_risk_max < 0.55
    assert 0.20 < mid.pass_prob_min < 0.30
    assert high.pit_confidence_min == pytest.approx(0.55)
    assert high.position_loss_risk_max == pytest.approx(0.55)
    assert high.pass_prob_min == pytest.approx(0.20)

    bal_high = advice_scale(1.0, 1.0, TH, settings.resolved_mindset(), source="track")
    assert bal_high.pit_confidence_min == pytest.approx(0.70)
    assert bal_high.position_loss_risk_max == pytest.approx(0.30)
    assert bal_high.pass_prob_min == pytest.approx(0.30)


def test_learned_rating_prefers_track_then_global() -> None:
    db = Database(":memory:")
    db.set_param(7, DRIVER_COMPOUND, RATING, 0.7, 4)
    db.set_param(GLOBAL_TRACK_ID, DRIVER_COMPOUND, RATING, 0.4, 10)

    assert learned_rating(db.params_for_track(7), db.params_for_track(GLOBAL_TRACK_ID), TH) == (
        0.7,
        pytest.approx(4 / 14),
        "track",
    )
    assert learned_rating([], db.params_for_track(GLOBAL_TRACK_ID), TH) == (
        0.4,
        pytest.approx(0.5),
        "global",
    )
    assert learned_rating([], [], TH) == (0.5, 0.0, "")


def _finish_session(
    db: Database,
    uid: int,
    *,
    track_id: int = 7,
    session_type: int = 15,
    lap_times: tuple[int, ...] = (89_000, 90_000, 91_000),
    rival_times: tuple[int, ...] = (90_000, 90_000, 90_000),
    grid: int = 8,
    finish: int = 5,
    event_telemetry_seen: bool = False,
    overtakes: tuple[int, int] = (0, 0),
    warnings: int = 0,
    penalties: int = 0,
    offs: int = 0,
) -> None:
    db.upsert_session(uid, track_id=track_id, session_type=session_type)
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    engine.state.session_uid = uid
    engine.state.track_id = track_id
    engine.state.session_type = session_type
    engine.state.grid_position = grid
    engine.state.position = finish
    engine.state.event_telemetry_seen = event_telemetry_seen
    engine.state.overtakes.made, engine.state.overtakes.lost = overtakes
    engine.state._warning_events = [(float(i), "cut") for i in range(warnings)]
    engine.state._penalty_events = [(float(i), 4) for i in range(penalties)]
    engine.state._incident_events = [(float(i), "off") for i in range(offs)]
    for lap_num, lap_time in enumerate(lap_times, start=1):
        db.insert_lap(
            uid,
            0,
            LapSummary(lap_num, lap_time, 30_000, 30_000, 17, lap_num, 10.0, True),
        )
    for car_idx in range(1, 4):
        for lap_num, lap_time in enumerate(rival_times, start=1):
            db.insert_lap(
                uid,
                car_idx,
                LapSummary(lap_num, lap_time, 30_000, 30_000, 17, lap_num, 10.0, True),
            )
    engine._end_session(uid, 1.0)  # noqa: SLF001


def _open_driver_session(db: Database, uid: int):
    db.upsert_session(uid, track_id=7, session_type=5)
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    engine.state.session_uid = uid
    engine.state.track_id = 7
    engine.state.session_type = 5
    engine.state.grid_position = 8
    engine.state.position = 5
    for lap_num, lap_time in enumerate((89_000, 90_000, 91_000), start=1):
        db.insert_lap(
            uid,
            0,
            LapSummary(lap_num, lap_time, 30_000, 30_000, 17, lap_num, 10.0, True),
        )
    for car_idx in range(1, 4):
        for lap_num in range(1, 4):
            db.insert_lap(
                uid,
                car_idx,
                LapSummary(lap_num, 90_000, 30_000, 30_000, 17, lap_num, 10.0, True),
            )
    return engine


def test_uid_change_without_send_folds_driver_rating() -> None:
    db = Database(":memory:")
    uid = 711
    engine = _open_driver_session(db, uid)

    engine.ingest.on_datagram(
        pack_packet(
            PacketId.SESSION,
            {"session_type": 5, "track_id": 7},
            session_uid=uid + 1,
            session_time=1.0,
        ),
        1.0,
    )

    assert db.driver_folded(uid)
    assert db.get_param(7, DRIVER_COMPOUND, RATING).weight == 1
    assert db.get_param(GLOBAL_TRACK_ID, DRIVER_COMPOUND, RATING).weight == 1


def test_send_then_uid_change_does_not_fold_driver_twice() -> None:
    db = Database(":memory:")
    uid = 712
    engine = _open_driver_session(db, uid)

    engine.ingest.on_datagram(make_event_packet(b"SEND", session_uid=uid), 1.0)
    engine.tick(1.0)
    engine.ingest.on_datagram(
        pack_packet(
            PacketId.SESSION,
            {"session_type": 5, "track_id": 7},
            session_uid=uid + 1,
            session_time=1.1,
        ),
        1.1,
    )

    assert db.driver_folded(uid)
    assert db.get_param(7, DRIVER_COMPOUND, RATING).weight == 1
    assert db.get_param(GLOBAL_TRACK_ID, DRIVER_COMPOUND, RATING).weight == 1


def test_session_end_folds_overtake_and_infringement_components() -> None:
    db = Database(":memory:")
    _finish_session(
        db,
        190,
        event_telemetry_seen=True,
        overtakes=(4, 1),
        warnings=1,
        penalties=1,
    )

    overtakes = db.get_param(7, DRIVER_COMPOUND, PARAM_NAMES["overtakes_per_lap"])
    infringements = db.get_param(7, DRIVER_COMPOUND, PARAM_NAMES["infringements_per_lap"])
    assert overtakes is not None and overtakes.value == pytest.approx(1.0)
    assert infringements is not None and infringements.value == pytest.approx(4 / 3)


def test_session_end_omits_event_components_without_event_telemetry() -> None:
    db = Database(":memory:")
    _finish_session(db, 191)

    assert db.get_param(7, DRIVER_COMPOUND, PARAM_NAMES["overtakes_per_lap"]) is None
    assert db.get_param(7, DRIVER_COMPOUND, PARAM_NAMES["infringements_per_lap"]) is None


def test_session_end_stores_zero_components_when_event_telemetry_has_no_events() -> None:
    db = Database(":memory:")
    _finish_session(db, 192, event_telemetry_seen=True)

    overtakes = db.get_param(7, DRIVER_COMPOUND, PARAM_NAMES["overtakes_per_lap"])
    infringements = db.get_param(7, DRIVER_COMPOUND, PARAM_NAMES["infringements_per_lap"])
    assert overtakes is not None and overtakes.value == 0.0
    assert infringements is not None and infringements.value == 0.0


def test_off_track_excursions_remain_errors_not_infringements() -> None:
    db = Database(":memory:")
    _finish_session(db, 193, event_telemetry_seen=True, offs=2)

    errors = db.get_param(7, DRIVER_COMPOUND, PARAM_NAMES["errors_per_lap"])
    infringements = db.get_param(7, DRIVER_COMPOUND, PARAM_NAMES["infringements_per_lap"])
    assert errors is not None and errors.value == pytest.approx(2 / 3)
    assert infringements is not None and infringements.value == 0.0


@pytest.mark.parametrize(("session_type", "expected_weight"), [(1, None), (5, 1), (15, 5)])
def test_session_end_folds_only_qualifying_and_race(
    session_type: int, expected_weight: float | None
) -> None:
    db = Database(":memory:")
    _finish_session(db, 100 + session_type, session_type=session_type)

    param = db.get_param(7, DRIVER_COMPOUND, RATING)
    if expected_weight is None:
        assert param is None
    else:
        assert param is not None
        assert param.weight == expected_weight


def test_session_end_does_not_mark_session_without_a_profile() -> None:
    db = Database(":memory:")
    uid = 201
    _finish_session(db, uid, session_type=5, lap_times=(89_000, 90_000))

    assert db.get_param(7, DRIVER_COMPOUND, RATING) is None
    assert not db.driver_folded(uid)


def test_track_and_global_folds_and_unseen_track_fallback() -> None:
    db = Database(":memory:")
    _finish_session(db, 301, track_id=7, lap_times=(88_000, 89_000, 90_000))
    _finish_session(db, 302, track_id=8, lap_times=(92_000, 93_000, 94_000), grid=5, finish=8)

    track_a = db.get_param(7, DRIVER_COMPOUND, RATING)
    track_b = db.get_param(8, DRIVER_COMPOUND, RATING)
    global_param = db.get_param(GLOBAL_TRACK_ID, DRIVER_COMPOUND, RATING)
    assert track_a is not None and track_b is not None and global_param is not None
    assert track_a.value != track_b.value
    assert global_param.weight == 10
    assert global_param.value == pytest.approx((track_a.value * 5 + track_b.value * 5) / 10)
    assert learned_rating([], db.params_for_track(GLOBAL_TRACK_ID), TH)[2] == "global"


def test_session_fold_deduplicates_unsigned_uid_after_restart() -> None:
    db = Database(":memory:")
    uid = (1 << 63) + 77
    _finish_session(db, uid)
    first = db.get_param(7, DRIVER_COMPOUND, RATING)
    assert first is not None and first.weight == 5
    assert db.driver_folded(uid)

    _finish_session(db, uid)
    second = db.get_param(7, DRIVER_COMPOUND, RATING)
    assert second is not None
    assert second.weight == 5
    assert second.value == first.value


def test_weighted_rating_evolves_across_sessions() -> None:
    db = Database(":memory:")
    cases = (
        ((88_000, 89_000, 90_000), (90_000, 90_000, 90_000)),
        ((90_000, 91_000, 92_000), (90_000, 90_000, 90_000)),
        ((92_000, 93_000, 94_000), (90_000, 90_000, 90_000)),
    )
    profile_ratings = []
    for index, (lap_times, rival_times) in enumerate(cases):
        profile = compute_profile(
            inputs(
                lap_times_ms=lap_times,
                field_lap_times_ms=rival_times,
                base_ms=None,
                deg_ms_per_lap=None,
                deg_prior_ms_per_lap=None,
                laps=3,
                lockups=0,
                spins=0,
                offs=0,
                contacts=0,
                grid_position=8,
                finish_position=5,
            ),
            TH,
        )
        assert profile is not None
        profile_ratings.append(profile.rating)
        _finish_session(
            db,
            401 + index,
            lap_times=lap_times,
            rival_times=rival_times,
        )

    learned = db.get_param(7, DRIVER_COMPOUND, RATING)
    assert learned is not None
    assert learned.weight == 15
    assert learned.value == pytest.approx(sum(profile_ratings) / len(profile_ratings))
    assert confidence(learned.weight, TH) > confidence(5, TH)


def test_global_driver_rows_survive_maintenance() -> None:
    db = Database(":memory:")
    db.set_param(GLOBAL_TRACK_ID, DRIVER_COMPOUND, RATING, 0.4, 5)
    db.set_param(GLOBAL_TRACK_ID, DRIVER_COMPOUND, "driver_pace_delta_ms", 100, 5)
    db.set_param(GLOBAL_TRACK_ID, 17, "deg_ms_per_lap", 80, 5)

    quarantine_bad(db, TH)
    assert db.get_param(GLOBAL_TRACK_ID, DRIVER_COMPOUND, RATING) is not None
    assert db.get_param(GLOBAL_TRACK_ID, DRIVER_COMPOUND, "driver_pace_delta_ms") is not None

    db.set_param(7, DRIVER_COMPOUND, RATING, 0.6, 5)
    rebuild_stint_params(db, TH)
    assert db.get_param(7, DRIVER_COMPOUND, RATING) is not None


def test_digest_includes_driver_rating_and_formats_missing_rating() -> None:
    db = Database(":memory:")
    db.upsert_session(501, track_id=7, session_type=15)
    db.set_param(7, DRIVER_COMPOUND, RATING, 0.62, 10)
    db.set_param(GLOBAL_TRACK_ID, DRIVER_COMPOUND, RATING, 0.4, 5)

    digest = build_digest(db, 501, TH)
    assert digest["version"] == DIGEST_VERSION == 4
    assert digest["driver"] == {
        "rating": 0.62,
        "confidence": 0.5,
        "source": "track",
        "track_weight": 10.0,
        "global_rating": 0.4,
        "global_confidence": pytest.approx(1 / 3),
    }
    assert "driver rating 0.62 (confidence 0.50, track)" in format_digest(digest)

    missing = build_digest(Database(":memory:"), 502, TH)
    assert "driver rating n/a" in format_digest(missing)
