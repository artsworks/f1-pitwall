"""Enums for format-2026 packets. Values from docs/reference/f1-26-udp-notes.md."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class SessionType(IntEnum):
    UNKNOWN = 0
    P1 = 1
    P2 = 2
    P3 = 3
    SHORT_PRACTICE = 4
    Q1 = 5
    Q2 = 6
    Q3 = 7
    SHORT_QUALIFYING = 8
    ONE_SHOT_QUALIFYING = 9
    SPRINT_SHOOTOUT_1 = 10
    SPRINT_SHOOTOUT_2 = 11
    SPRINT_SHOOTOUT_3 = 12
    SHORT_SPRINT_SHOOTOUT = 13
    ONE_SHOT_SPRINT_SHOOTOUT = 14
    RACE = 15
    RACE_2 = 16
    RACE_3 = 17
    TIME_TRIAL = 18

    def kind(self) -> str:
        """docs/03 mapping: 1-4 practice, 5-9 qualifying, 10-14 sprint shootout
        (quali rule set), 15-17 race, 18 time trial."""
        if 1 <= self <= 4:
            return "practice"
        if 5 <= self <= 9:
            return "qualifying"
        if 10 <= self <= 14:
            return "sprint_shootout"
        if 15 <= self <= 17:
            return "race"
        if self == 18:
            return "time_trial"
        return "unknown"


# Sparse: 1, 8, 18, 21-25, 28 and 33-38 are absent in F1 26.
class TrackId(IntEnum):
    MELBOURNE = 0
    SHANGHAI = 2
    SAKHIR = 3
    CATALUNYA = 4
    MONACO = 5
    MONTREAL = 6
    SILVERSTONE = 7
    HUNGARORING = 9
    SPA = 10
    MONZA = 11
    SINGAPORE = 12
    SUZUKA = 13
    ABU_DHABI = 14
    TEXAS = 15
    BRAZIL = 16
    AUSTRIA = 17
    MEXICO = 19
    BAKU = 20
    ZANDVOORT = 26
    IMOLA = 27
    JEDDAH = 29
    MIAMI = 30
    LAS_VEGAS = 31
    LOSAIL = 32
    SILVERSTONE_REVERSE = 39
    AUSTRIA_REVERSE = 40
    ZANDVOORT_REVERSE = 41
    MADRID = 42


class ActualCompound(IntEnum):
    C6 = 22
    C5 = 16
    C4 = 17
    C3 = 18
    C2 = 19
    C1 = 20
    C0 = 21
    INTER = 7
    WET = 8


class VisualCompound(IntEnum):
    SOFT = 16
    MEDIUM = 17
    HARD = 18
    INTER = 7
    WET = 8


class DriverStatus(IntEnum):
    IN_GARAGE = 0
    FLYING_LAP = 1
    IN_LAP = 2
    OUT_LAP = 3
    ON_TRACK = 4


class PitStatus(IntEnum):
    NONE = 0
    PITTING = 1
    IN_PIT_AREA = 2


class SafetyCarStatus(IntEnum):
    NONE = 0
    FULL = 1
    VIRTUAL = 2
    FORMATION_LAP = 3


class ResultStatus(IntEnum):
    INVALID = 0
    INACTIVE = 1
    ACTIVE = 2
    FINISHED = 3
    DNF = 4
    DSQ = 5
    NOT_CLASSIFIED = 6
    RETIRED = 7


class EventCode(StrEnum):
    SESSION_STARTED = "SSTA"
    SESSION_ENDED = "SEND"
    FASTEST_LAP = "FTLP"
    RETIREMENT = "RTMT"
    DRS_ENABLED = "DRSE"
    DRS_DISABLED = "DRSD"
    TEAM_MATE_IN_PITS = "TMPT"
    CHEQUERED_FLAG = "CHQF"
    RACE_WINNER = "RCWN"
    PENALTY = "PENA"
    SPEED_TRAP = "SPTP"
    START_LIGHTS = "STLG"
    LIGHTS_OUT = "LGOT"
    DRIVE_THROUGH_SERVED = "DTSV"
    STOP_GO_SERVED = "SGSV"
    FLASHBACK = "FLBK"
    BUTTON_STATUS = "BUTN"
    RED_FLAG = "RDFL"
    OVERTAKE = "OVTK"
    SAFETY_CAR = "SCAR"
    COLLISION = "COLL"
