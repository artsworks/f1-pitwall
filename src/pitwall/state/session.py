"""SessionState: single mutable model updated per packet; Snapshot is the
frozen view rules read. Player car only for M1 (24-slot arrays kept)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pitwall.protocol.enums import DriverStatus, PitStatus, SessionType
from pitwall.protocol.header import PacketHeader, PacketId
from pitwall.protocol.layouts import Corners
from pitwall.protocol.packets import (
    CarDamagePacket,
    CarStatusPacket,
    CarTelemetryPacket,
    EventPacket,
    LapDataPacket,
    SessionPacket,
    parse,
)
from pitwall.state.ema import CornersEma
from pitwall.state.lap import LapAccumulator, LapSummary

PACKET_NAMES: dict[int, str] = {
    PacketId.SESSION: "session",
    PacketId.LAP_DATA: "lap_data",
    PacketId.EVENT: "event",
    PacketId.CAR_TELEMETRY: "car_telemetry",
    PacketId.CAR_STATUS: "car_status",
    PacketId.CAR_DAMAGE: "car_damage",
}

_ZERO_CORNERS = Corners(0.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class Damage:
    """Player car damage (percent unless a fault flag)."""

    front_left_wing: int = 0
    front_right_wing: int = 0
    rear_wing: int = 0
    floor: int = 0
    diffuser: int = 0
    sidepod: int = 0
    gearbox: int = 0
    engine: int = 0
    drs_fault: int = 0
    ers_fault: int = 0


_ZERO_DAMAGE = Damage()

# session_time regression larger than this counts as a flashback/rewind.
REWIND_THRESHOLD_S = 1.0

_PHASE_BY_DRIVER_STATUS = {
    DriverStatus.IN_GARAGE: "garage",
    DriverStatus.FLYING_LAP: "flying",
    DriverStatus.IN_LAP: "in_lap",
    DriverStatus.OUT_LAP: "out_lap",
    DriverStatus.ON_TRACK: "on_track",
}


@dataclass(slots=True)
class Snapshot:
    """Frozen per-tick view; everything rules read lives here."""

    now: float
    session_time: float = 0.0
    last_packet_t: float | None = None  # clock time of newest packet, same base as `now`
    session_kind: str = "unknown"
    session_type: int = 0
    track_id: int = -1
    total_laps: int = 0
    lap_num: int = 0
    lap_distance: float = 0.0
    sector: int = 0
    position: int = 0
    driver_status: int = 0
    pit_status: int = 0
    phase: str = "garage"
    safety_car_status: int = 0
    tyre_surface: Corners = _ZERO_CORNERS
    tyre_inner: Corners = _ZERO_CORNERS
    brake_temp: Corners = _ZERO_CORNERS
    tyre_surface_ema_fast: Corners = _ZERO_CORNERS
    tyre_surface_ema_slow: Corners = _ZERO_CORNERS
    tyre_inner_ema_fast: Corners = _ZERO_CORNERS
    tyre_inner_ema_slow: Corners = _ZERO_CORNERS
    brake_ema_fast: Corners = _ZERO_CORNERS
    brake_ema_slow: Corners = _ZERO_CORNERS
    tyre_compound: int = 0
    tyre_visual: int = 0
    tyre_age_laps: int = 0
    fuel_remaining_laps: float = 0.0
    fuel_in_tank: float = 0.0
    ers_store_pct: float = 0.0
    ers_deploy_mode: int = 0
    drs_allowed: int = 0
    tyres_wear: Corners = _ZERO_CORNERS
    damage: Damage = _ZERO_DAMAGE
    laps: tuple[LapSummary, ...] = ()
    _ages: dict[str, float] = field(default_factory=dict)

    def age(self, packet_name: str) -> float:
        """Seconds since the named source packet last updated the snapshot."""
        return self._ages.get(packet_name, float("inf"))


class SessionState:
    """Mutable session model. Handlers are registered on Ingest per packet id."""

    def __init__(self, ema_fast_s: float = 3.0, ema_slow_s: float = 30.0) -> None:
        self._last_update: dict[str, float] = {}  # packet name -> session_time
        self._last_session_time: float | None = None
        self.last_packet_t: float | None = None
        self.last_recv_wall: float | None = None
        self._player_idx = 0

        # session context
        self.session_type = 0
        self.track_id = -1
        self.total_laps = 0
        self.safety_car_status = 0

        # player lap data
        self.lap_num = 0
        self.lap_distance = 0.0
        self.sector = 0
        self.position = 0
        self.driver_status = 0
        self.pit_status = 0

        # telemetry / status / damage (player)
        self.tyre_surface = _ZERO_CORNERS
        self.tyre_inner = _ZERO_CORNERS
        self.brake_temp = _ZERO_CORNERS
        self.tyre_compound = 0
        self.tyre_visual = 0
        self.tyre_age_laps = 0
        self.fuel_remaining_laps = 0.0
        self.fuel_in_tank = 0.0
        self.ers_store_pct = 0.0
        self.ers_deploy_mode = 0
        self.drs_allowed = 0
        self.tyres_wear = _ZERO_CORNERS
        self.damage = _ZERO_DAMAGE

        self.tyre_surface_fast = CornersEma(ema_fast_s)
        self.tyre_surface_slow = CornersEma(ema_slow_s)
        self.tyre_inner_fast = CornersEma(ema_fast_s)
        self.tyre_inner_slow = CornersEma(ema_slow_s)
        self.brake_fast = CornersEma(ema_fast_s)
        self.brake_slow = CornersEma(ema_slow_s)

        self.lap_acc = LapAccumulator()
        self.laps: list[LapSummary] = []

        self.cars_lap: tuple[Any, ...] | None = None
        self.cars_telemetry: tuple[Any, ...] | None = None
        self.cars_status: tuple[Any, ...] | None = None
        self.cars_damage: tuple[Any, ...] | None = None

    # -- ingest entry point -----------------------------------------------

    def register(self, ingest: Any) -> None:
        for pid in PACKET_NAMES:
            ingest.register(pid, self.on_packet)

    def on_packet(self, header: PacketHeader, payload: bytes, recv_time: float = 0.0) -> None:
        pid = header.packet_id
        self._player_idx = header.player_car_index
        self.last_packet_t = header.session_time
        self.last_recv_wall = recv_time
        st = header.session_time
        if (
            self._last_session_time is not None
            and st < self._last_session_time - REWIND_THRESHOLD_S
        ):
            self._handle_rewind()
        self._last_session_time = st
        name = PACKET_NAMES.get(pid)
        if name is not None:
            self._last_update[name] = st
        try:
            pkt = parse(pid, payload, header)
        except KeyError:
            return
        if isinstance(pkt, SessionPacket):
            self._on_session(pkt)
        elif isinstance(pkt, LapDataPacket):
            self._on_lap_data(pkt)
        elif isinstance(pkt, EventPacket):
            self._on_event(pkt)
        elif isinstance(pkt, CarTelemetryPacket):
            self._on_car_telemetry(pkt, st)
        elif isinstance(pkt, CarStatusPacket):
            self._on_car_status(pkt)
        elif isinstance(pkt, CarDamagePacket):
            self._on_car_damage(pkt)

    def _handle_rewind(self) -> None:
        for ema in (
            self.tyre_surface_fast,
            self.tyre_surface_slow,
            self.tyre_inner_fast,
            self.tyre_inner_slow,
            self.brake_fast,
            self.brake_slow,
        ):
            ema.reset()
        self.lap_acc.note_flashback()

    # -- per-packet updates -------------------------------------------------

    def _on_session(self, pkt: SessionPacket) -> None:
        self.session_type = pkt.session_type
        self.track_id = pkt.track_id
        self.total_laps = pkt.total_laps
        self.safety_car_status = pkt.safety_car_status

    def _on_lap_data(self, pkt: LapDataPacket) -> None:
        self.cars_lap = pkt.cars
        car = pkt.cars[self._player_idx]
        self.lap_num = car.current_lap_num
        self.lap_distance = car.lap_distance
        self.sector = car.sector
        self.position = car.car_position
        self.driver_status = car.driver_status
        self.pit_status = car.pit_status
        summary = self.lap_acc.update(
            current_lap_num=car.current_lap_num,
            last_lap_time_ms=car.last_lap_time_ms,
            sector1_ms=car.sector1_ms,
            sector2_ms=car.sector2_ms,
            pit_status=car.pit_status,
            driver_status=car.driver_status,
            current_lap_invalid=car.current_lap_invalid,
            safety_car_status=self.safety_car_status,
            compound=self.tyre_compound,
            tyre_age_laps=self.tyre_age_laps,
            fuel_remaining_laps=self.fuel_remaining_laps,
        )
        if summary is not None:
            self.laps.append(summary)

    def _on_event(self, pkt: EventPacket) -> None:
        if pkt.code == "FLBK":
            self._handle_rewind()

    def _on_car_telemetry(self, pkt: CarTelemetryPacket, st: float) -> None:
        self.cars_telemetry = pkt.cars
        car = pkt.cars[self._player_idx]
        self.tyre_surface = car.tyres_surface_temperature
        self.tyre_inner = car.tyres_inner_temperature
        self.brake_temp = car.brakes_temperature
        self.tyre_surface_fast.update(st, car.tyres_surface_temperature)
        self.tyre_surface_slow.update(st, car.tyres_surface_temperature)
        self.tyre_inner_fast.update(st, car.tyres_inner_temperature)
        self.tyre_inner_slow.update(st, car.tyres_inner_temperature)
        self.brake_fast.update(st, car.brakes_temperature)
        self.brake_slow.update(st, car.brakes_temperature)

    def _on_car_status(self, pkt: CarStatusPacket) -> None:
        self.cars_status = pkt.cars
        car = pkt.cars[self._player_idx]
        self.tyre_compound = car.actual_tyre_compound
        self.tyre_visual = car.visual_tyre_compound
        self.tyre_age_laps = car.tyres_age_laps
        self.fuel_remaining_laps = car.fuel_remaining_laps
        self.fuel_in_tank = car.fuel_in_tank
        cap = 4_000_000.0  # nominal 4 MJ ERS store
        self.ers_store_pct = min(100.0, car.ers_store_energy / cap * 100.0)
        self.ers_deploy_mode = car.ers_deploy_mode
        self.drs_allowed = car.drs_allowed

    def _on_car_damage(self, pkt: CarDamagePacket) -> None:
        self.cars_damage = pkt.cars
        car = pkt.cars[self._player_idx]
        self.tyres_wear = car.tyres_wear
        self.damage = Damage(
            front_left_wing=car.front_left_wing_damage,
            front_right_wing=car.front_right_wing_damage,
            rear_wing=car.rear_wing_damage,
            floor=car.floor_damage,
            diffuser=car.diffuser_damage,
            sidepod=car.sidepod_damage,
            gearbox=car.gearbox_damage,
            engine=car.engine_damage,
            drs_fault=car.drs_fault,
            ers_fault=car.ers_fault,
        )

    # -- snapshot -----------------------------------------------------------

    def snapshot(self, now: float) -> Snapshot:
        st = self._last_session_time or 0.0
        try:
            kind = SessionType(self.session_type).kind()
        except ValueError:
            kind = "unknown"
        phase = self._phase()
        return Snapshot(
            now=now,
            session_time=st,
            last_packet_t=self.last_recv_wall,
            session_kind=kind,
            session_type=self.session_type,
            track_id=self.track_id,
            total_laps=self.total_laps,
            lap_num=self.lap_num,
            lap_distance=self.lap_distance,
            sector=self.sector,
            position=self.position,
            driver_status=self.driver_status,
            pit_status=self.pit_status,
            phase=phase,
            safety_car_status=self.safety_car_status,
            tyre_surface=self.tyre_surface,
            tyre_inner=self.tyre_inner,
            brake_temp=self.brake_temp,
            tyre_surface_ema_fast=self._ema_or_zero(self.tyre_surface_fast),
            tyre_surface_ema_slow=self._ema_or_zero(self.tyre_surface_slow),
            tyre_inner_ema_fast=self._ema_or_zero(self.tyre_inner_fast),
            tyre_inner_ema_slow=self._ema_or_zero(self.tyre_inner_slow),
            brake_ema_fast=self._ema_or_zero(self.brake_fast),
            brake_ema_slow=self._ema_or_zero(self.brake_slow),
            tyre_compound=self.tyre_compound,
            tyre_visual=self.tyre_visual,
            tyre_age_laps=self.tyre_age_laps,
            fuel_remaining_laps=self.fuel_remaining_laps,
            fuel_in_tank=self.fuel_in_tank,
            ers_store_pct=self.ers_store_pct,
            ers_deploy_mode=self.ers_deploy_mode,
            drs_allowed=self.drs_allowed,
            tyres_wear=self.tyres_wear,
            damage=self.damage,
            laps=tuple(self.laps),
            _ages={name: st - t for name, t in self._last_update.items()},
        )

    def _phase(self) -> str:
        if self.pit_status in (PitStatus.PITTING, PitStatus.IN_PIT_AREA):
            return "pitting"
        try:
            return _PHASE_BY_DRIVER_STATUS[DriverStatus(self.driver_status)]
        except (ValueError, KeyError):
            return "on_track"

    @staticmethod
    def _ema_or_zero(ema: CornersEma) -> Corners:
        vals = [e.value if e.value is not None else 0.0 for e in ema.emas.as_tuple()]
        return Corners(*vals)
