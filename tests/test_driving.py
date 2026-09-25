from __future__ import annotations

import math

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId
from pitwall.protocol.layouts import Corners
from pitwall.rules.engine import RuleEngine
from pitwall.state.driving import BoostTimer, LockupDetector, YellowTracker
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
