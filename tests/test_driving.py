from __future__ import annotations

import math

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId
from pitwall.protocol.layouts import Corners
from pitwall.rules.engine import RuleEngine
from pitwall.state.driving import BoostTimer, LockupDetector, SpinDetector, YellowTracker
from pitwall.state.session import SessionState, Snapshot

from .synth import pack_packet

FREE = Corners(-0.02, -0.02, -0.02, -0.02)


def _slip(**kw: float) -> Corners:
    v = {"rl": -0.02, "rr": -0.02, "fl": -0.02, "fr": -0.02, **kw}
    return Corners(v["rl"], v["rr"], v["fl"], v["fr"])


def _run(det: LockupDetector, locked: Corners, seconds: float, t0: float = 0.0) -> float:
    t = t0
    while t < t0 + seconds:
        det.update(t, locked, 150.0, 0.8, 3)
        t += 1 / 30
    for _ in range(6):
        det.update(t, FREE, 120.0, 0.8, 3)
        t += 1 / 30
    return t


def test_front_lockup_reported_after_release() -> None:
    det = LockupDetector()
    det.update(0.0, _slip(fl=-1.0), 150.0, 0.8, 3)
    assert det.recent(0.0) == ("", "")
    t = _run(det, _slip(fl=-1.0), 0.5)
    assert det.recent(t) == ("front", "front left")
    assert det.count_lap == 1
    assert det.recent(t + 5) == ("", "")


def test_short_slip_is_not_a_lockup() -> None:
    det = LockupDetector()
    t = _run(det, _slip(fr=-0.9), 0.1)
    assert det.recent(t) == ("", "")
    assert det.count_lap == 0


def test_rear_lockup_and_low_speed_ignored() -> None:
    det = LockupDetector()
    t = _run(det, _slip(rl=-0.6, fl=-0.4), 0.4)
    assert det.recent(t) == ("rear", "rear left")
    slow = LockupDetector()
    for i in range(30):
        slow.update(i / 30, _slip(fl=-1.0), 20.0, 1.0, 3)
    slow.update(1.2, FREE, 0.0, 1.0, 3)
    assert slow.recent(1.2) == ("", "")


def test_boost_timer() -> None:
    b = BoostTimer()
    b.update(0.0, 3)
    b.update(4.0, 3)
    assert b.on_s(4.0) == pytest.approx(4.0)
    b.update(4.1, 1)
    assert b.on_s(4.1) == 0.0


def _tracker(car_m: float, flags: list[int], prev: YellowTracker | None = None) -> YellowTracker:
    y = prev or YellowTracker()
    # 4 zones on a 4000 m lap: 0-1000, 1000-2000, 2000-3000, 3000-4000
    y.update([0.0, 0.25, 0.5, 0.75], flags, 4000.0, 1500.0, 2800.0, car_m)
    return y


def test_yellow_ahead_with_sector() -> None:
    y = _tracker(500.0, [0, 0, 0, 0])
    y = _tracker(1400.0, [0, 0, 3, 0], y)
    v = y.view(1400.0)
    assert v.ahead_m == pytest.approx(600.0)
    assert v.ahead_sector == 2
    assert math.isinf(v.behind_m)
    assert y.view(2100.0).here


def test_yellow_behind_reported_then_becomes_ahead() -> None:
    y = _tracker(2500.0, [0, 3, 0, 0])
    v = y.view(2500.0)
    assert v.behind_m == pytest.approx(500.0)
    assert v.behind_sector == 1
    assert math.isinf(v.ahead_m)
    v = y.view(500.0)  # came round: now ahead
    assert v.ahead_m == pytest.approx(500.0)


def test_own_yellow_ignored() -> None:
    y = _tracker(1200.0, [0, 3, 0, 0])
    for m in (1200.0, 2500.0, 500.0):
        v = y.view(m)
        assert (v.here, v.ahead_m, v.behind_m) == (False, math.inf, math.inf)
    y = _tracker(1200.0, [0, 0, 0, 0], y)
    y = _tracker(500.0, [0, 3, 0, 0], y)  # a new yellow in the same zone counts
    assert y.view(500.0).ahead_m == pytest.approx(500.0)


# -- shipped rules -------------------------------------------------------

AGES = {
    n: 0.1
    for n in ("session", "lap_data", "car_telemetry", "car_status", "car_damage", "motion_ex")
}


def _engine() -> RuleEngine:
    s = ConfigStore().current()
    return RuleEngine(
        s.rules,
        thresholds=s.thresholds,
        mode=s.resolved_mindset(),
        staleness_s=s.engine.staleness_s,
    )


def _texts(engine: RuleEngine, **kw: object) -> dict[str, str]:
    base: dict[str, object] = {
        "now": 0.0,
        "session_kind": "race",
        "lap_num": 5,
        "phase": "flying",
        "throttle": 1.0,
        "_ages": AGES,
    }
    base.update(kw)
    res = engine.evaluate(Snapshot(**base))  # type: ignore[arg-type]
    return {c.rule.id: c.text for c in res.candidates}


def test_boost_rule_fires_on_lift_not_on_straight() -> None:
    e = _engine()
    assert "boost_left_on" not in _texts(e, boost_on_s=8.0)
    assert _texts(e, boost_on_s=8.0, throttle=0.2)["boost_left_on"].startswith("Boost still on")
    assert "boost_left_on" not in _texts(e, boost_on_s=9.0, brake=0.9)  # not re-armed
    _texts(e, boost_on_s=0.0)
    assert "boost_left_on" in _texts(e, boost_on_s=13.0)


def test_lockup_rules_text() -> None:
    e = _engine()
    t = _texts(e, lockup="rear", lockup_wheel="rear right", front_brake_bias=54)
    assert t["lockup_rear"] == (
        "Rears locking, rear right. Move the brake bias forward, you're on 54."
    )
    t = _texts(e, lockup="front", lockup_wheel="front left")
    assert t["lockup_front"].startswith("Lock-up, front left.")


def test_yellow_rules_text() -> None:
    e = _engine()
    t = _texts(e, yellow_ahead_m=450.0, yellow_ahead_sector=2)
    assert t["yellow_ahead"] == "Yellow ahead, sector 2, 450 metres. Careful, no overtaking."
    t = _texts(e, yellow_behind_m=600.0, yellow_behind_sector=1)
    assert t["yellow_behind"] == "Yellow behind you in sector 1. You're clear, ignore it."
    assert _texts(e) == {}


def test_out_lap_rules_speak_in_s3_only() -> None:
    e = _engine()
    cold = {"phase": "out_lap", "s3_entry_coldest_c": 60.0, "coldest_tyre_c": 60.0}
    assert _texts(e, sector=1, **cold) == {}
    assert list(_texts(e, sector=2, **cold)) == ["out_lap_s3_tyres_cold"]
    e = _engine()
    t = _texts(e, phase="out_lap", sector=2, s3_entry_coldest_c=85.0)
    assert list(t) == ["out_lap_s3_tyres_ready"]


# -- state wiring ----------------------------------------------------------


def test_state_lockup_from_motion_ex_and_bias() -> None:
    ingest = Ingest()
    state = SessionState()
    state.register(ingest)
    t = 0.0
    ingest.on_datagram(
        pack_packet(PacketId.CAR_STATUS, {"cars": {0: {"front_brake_bias": 56}}}, session_time=t),
        t,
    )
    for i in range(20):
        t = i / 30
        ingest.on_datagram(
            pack_packet(
                PacketId.CAR_TELEMETRY, {"cars": {0: {"speed": 200, "brake": 0.9}}}, session_time=t
            ),
            t,
        )
        slip = (-0.8, -0.1, -0.1, -0.1) if i < 12 else (0.0, 0.0, 0.0, 0.0)
        ingest.on_datagram(
            pack_packet(PacketId.MOTION_EX, {"wheel_slip_ratio": slip}, session_time=t), t
        )
    snap = state.snapshot(t)
    assert (snap.lockup, snap.lockup_wheel, snap.front_brake_bias) == ("rear", "rear left", 56)
    assert snap.lockups_this_lap == 1


def test_state_yellow_from_session_packet() -> None:
    ingest = Ingest()
    state = SessionState()
    state.register(ingest)

    def session(flag: int, t: float) -> bytes:
        zones = {i: {"zone_start": i / 4, "zone_flag": flag if i == 2 else 0} for i in range(4)}
        return pack_packet(
            PacketId.SESSION,
            {
                "track_length": 4000,
                "num_marshal_zones": 4,
                "marshal_zones": zones,
                "sector2_lap_distance_start": 1500.0,
                "sector3_lap_distance_start": 2800.0,
            },
            session_time=t,
        )

    lap = pack_packet(PacketId.LAP_DATA, {"cars": {0: {"lap_distance": 1400.0}}}, session_time=0.0)
    ingest.on_datagram(lap, 0.0)
    ingest.on_datagram(session(0, 0.0), 0.0)
    ingest.on_datagram(session(3, 0.5), 0.5)
    snap = state.snapshot(0.5)
    assert (snap.yellow_ahead_m, snap.yellow_ahead_sector) == (600.0, 2)


# -- phrasing and spins ------------------------------------------------------


def _lockup_texts(e: RuleEngine, n: int) -> list[str]:
    out = []
    for i in range(n):
        out.append(
            _texts(e, now=i * 10.0, lockup="front", lockup_wheel="front left")["lockup_front"]
        )
        _texts(e, now=i * 10.0 + 5)
    return out


def test_lockup_phrasing_rotates_then_escalates() -> None:
    texts = _lockup_texts(_engine(), 8)
    assert texts[0].startswith("Lock-up, front left.")
    assert texts[1] != texts[0]
    assert all(a != b for a, b in zip(texts, texts[1:], strict=False))
    calm = {t for t in texts[:2]}
    assert not calm & set(texts[2:])  # 3rd trigger inside the window switches pool
    assert "Lock-up number 6." in texts[5] or texts[5] in {
        "Again. Brake earlier, it's cheaper than a new set.",
        "You know what I'm going to say.",
    }


def test_phrasing_is_reproducible_across_runs() -> None:
    assert _lockup_texts(_engine(), 8) == _lockup_texts(_engine(), 8)


def test_repeat_window_resets_tone() -> None:
    e = _engine()
    for i in range(3):
        _texts(e, now=i * 10.0, lockup="front", lockup_wheel="front left")
        _texts(e, now=i * 10.0 + 5)
    t = _texts(e, now=2000.0, lockup="front", lockup_wheel="front left")["lockup_front"]
    assert "front left" in t


def _spin_run(det: SpinDetector, slip_deg: float, speed_kmh: float, t: float, s: float) -> float:
    v = speed_kmh / 3.6
    vel = (v * math.sin(math.radians(slip_deg)), 0.0, v * math.cos(math.radians(slip_deg)))
    end = t + s
    while t < end:
        det.update(t, vel)
        t += 1 / 30
    return t


def test_spin_reported_on_slow_recovery_not_on_caught_slide() -> None:
    det = SpinDetector()
    t = _spin_run(det, 10.0, 150.0, 0.0, 2.0)
    t = _spin_run(det, 50.0, 140.0, t, 0.5)
    t = _spin_run(det, 5.0, 130.0, t, 1.0)  # caught at speed
    assert not det.recent(t) and det.count == 0
    t = _spin_run(det, 120.0, 80.0, t, 0.5)
    t = _spin_run(det, 180.0, 40.0, t, 1.0)
    assert not det.recent(t)  # still going round
    t = _spin_run(det, 5.0, 30.0, t, 1.0)
    assert det.recent(t) and det.count == 1
    t = _spin_run(det, 5.0, 60.0, t, 5.0)
    assert not det.recent(t) and det.count == 1


def test_lockup_same_braking_zone_on_later_lap() -> None:
    det = LockupDetector()
    for lap, dist in ((1, 2770.0), (1, 3255.0), (2, 2790.0), (3, 2760.0), (3, 500.0)):
        t = lap * 100.0 + dist / 100
        while t < lap * 100.0 + dist / 100 + 0.5:
            det.update(t, _slip(fl=-1.0), 150.0, 0.8, lap, dist)
            t += 1 / 30
        for _ in range(6):
            det.update(t, FREE, 120.0, 0.8, lap, dist)
            t += 1 / 30
        got = det.spot_laps
        expected = {(1, 2770.0): 0, (1, 3255.0): 0, (2, 2790.0): 1, (3, 2760.0): 2, (3, 500.0): 0}
        assert got == expected[(lap, dist)]


def test_same_spot_rule_replaces_generic_lockup_call() -> None:
    t = _texts(_engine(), lockup="front", lockup_wheel="front left", lockup_spot_laps=1)
    assert list(t) == ["lockup_front_same_spot"]
    assert t["lockup_front_same_spot"].startswith("Locking up in the same braking zone")


def test_spin_rule_speaks_and_escalates() -> None:
    e = _engine()
    first = _texts(e, now=0.0, spun=True)["spun_rejoin"]
    assert first.startswith("You're clear behind") and "throttle" in first
    _texts(e, now=10.0)
    second = _texts(e, now=20.0, spun=True, spins=2)["spun_rejoin"]
    assert (
        second != first and "gentle" in second.lower() or "moments" in second or "Number" in second
    )


def test_spin_with_traffic_behind_says_hold_then_clear() -> None:
    e = _engine()
    held = _texts(e, now=0.0, spun=True, traffic_behind_s=3.0)
    assert "spun_rejoin_traffic" in held and "spun_rejoin" not in held
    assert "3" in held["spun_rejoin_traffic"]
    # car has passed while still recovering: now the clear-to-rejoin call
    clear = _texts(e, now=2.0, spun=True, traffic_behind_s=float("inf"))
    assert "spun_rejoin" in clear and "spun_rejoin_traffic" not in clear


def test_out_lap_gap_calls() -> None:
    base: dict[str, object] = {
        "session_kind": "qualifying",
        "phase": "out_lap",
        "dist_to_line_m": 500.0,
    }
    tow = _texts(_engine(), **base, traffic_ahead_kind="flying", traffic_ahead_s=1.2)
    assert "prep_tow" in tow and "1.2" in tow["prep_tow"]
    slow = _texts(_engine(), **base, traffic_ahead_kind="out_lap", traffic_ahead_s=2.0)
    assert "prep_traffic_ahead" in slow and "prep_tow" not in slow
    dirty = _texts(_engine(), **base, traffic_ahead_kind="flying", traffic_ahead_s=0.4)
    assert "prep_dirty_air" in dirty
    behind = _texts(_engine(), **base, traffic_behind_kind="flying", traffic_behind_s=2.0)
    assert "prep_car_behind" in behind and "prep_clear" not in behind
    clear = _texts(_engine(), **base)
    assert "prep_clear" in clear
    early = _texts(_engine(), **{**base, "dist_to_line_m": 2500.0})
    assert not any(k.startswith("prep_") for k in early)


def test_pit_exit_traffic() -> None:
    fired = _texts(_engine(), phase="out_lap", pit_exit_s=1.0, traffic_behind_s=2.5)
    assert "pit_exit_traffic" in fired
    late = _texts(_engine(), phase="out_lap", pit_exit_s=20.0, traffic_behind_s=2.5)
    assert "pit_exit_traffic" not in late


def test_slow_car_ahead_on_hot_lap() -> None:
    base: dict[str, object] = {"session_kind": "qualifying", "run_lap_kind": "hot"}
    warn = _texts(
        _engine(),
        **base,
        traffic_ahead_kind="in_lap",
        traffic_ahead_closing_s=3.0,
        traffic_ahead_m=220.0,
    )
    assert "slow_car_ahead" in warn and "220" in warn["slow_car_ahead"]
    # same-pace flying car ahead: no warning; a much slower one: warning
    assert "slow_car_ahead" not in _texts(
        _engine(), **base, traffic_ahead_kind="flying", traffic_ahead_closing_s=3.0
    )
    assert "slow_car_ahead" in _texts(
        _engine(),
        **base,
        traffic_ahead_kind="flying",
        traffic_ahead_closing_s=3.0,
        traffic_ahead_slow=True,
    )
    assert "slow_car_ahead" not in _texts(
        _engine(),
        **{**base, "run_lap_kind": "cool"},
        traffic_ahead_kind="in_lap",
        traffic_ahead_closing_s=3.0,
    )


def test_invalid_hot_lap_calls() -> None:
    base: dict[str, object] = {
        "session_kind": "qualifying",
        "run_lap_kind": "hot",
        "current_lap_invalid": 1,
    }
    t = _texts(_engine(), **base, time_for_cool_and_hot=True)
    assert "lap_deleted" in t and "lap_deleted_last" not in t
    t = _texts(_engine(), **base, time_for_cool_and_hot=False)
    assert "lap_deleted_last" in t and "lap_deleted" not in t


def test_cool_lap_extends_when_battery_short() -> None:
    base: dict[str, object] = {
        "session_kind": "qualifying",
        "cool_lap": True,
        "cool_prep": True,
        "run_lap_kind": "cool",
        "ers_store_pct": 34.0,
        "ers_need_pct": 60.0,
    }
    t = _texts(_engine(), **base, cool_extend=True)
    assert "cool_extend" in t and "cool_hot_mode" not in t and "60" in t["cool_extend"]
    t = _texts(_engine(), **base, cool_extend=False)
    assert "cool_hot_mode" in t and "cool_extend" not in t
