"""Spoken-slot vocabulary for the voice lab and later phrase prewarming."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from pitwall.config.models import RuleDefModel

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_words(n: int) -> str:
    """Spoken form of an integer from 0 to 99."""
    if not 0 <= n <= 99:
        raise ValueError(f"number out of range: {n}")
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


DRIVER_NAMES = [
    "Verstappen",
    "Hadjar",
    "Leclerc",
    "Hamilton",
    "Norris",
    "Piastri",
    "Russell",
    "Antonelli",
    "Alonso",
    "Stroll",
    "Gasly",
    "Colapinto",
    "Albon",
    "Sainz",
    "Lawson",
    "Lindblad",
    "Ocon",
    "Bearman",
    "Hulkenberg",
    "Bortoleto",
    "Perez",
    "Bottas",
]

# Hardcoded because the repo has no team id to name table (no id map exists in
# protocol/enums.py or layouts.py, and F1 26 team ids are unverified).
TEAM_NAMES = [
    "Mercedes",
    "Ferrari",
    "Red Bull",
    "Williams",
    "Aston Martin",
    "Alpine",
    "Racing Bulls",
    "Haas",
    "McLaren",
    "Audi",
    "Cadillac",
]

POSITIONS = [f"P{n}" for n in range(1, 23)]

TEMPS = (
    [number_words(n) for n in range(70, 100)]
    + ["one hundred"]
    + [f"one oh {_ONES[i]}" for i in range(1, 6)]
)

DIGITS = [*_ONES[:10], "point"]

LAP_TIME_ATOMS = list(
    dict.fromkeys(
        [
            "one minute",
            "two minutes",
            *(number_words(n) for n in range(1, 60)),
            "point",
            *_ONES[:10],
            "seconds",
        ]
    )
)


def lap_time_atoms(ms: float) -> list[str]:
    """Atoms that speak a lap time in milliseconds, mirroring spoken_lap_time."""
    tenths = round(ms / 100)
    minutes, rem = divmod(tenths, 600)
    whole, tenth = divmod(rem, 10)
    atoms: list[str] = []
    if minutes > 0:
        if minutes == 1:
            atoms.append("one minute")
        elif minutes == 2:
            atoms.append("two minutes")
        else:
            atoms.append(f"{number_words(minutes)} minutes")
    atoms.append(number_words(whole))
    if tenth != 0:
        atoms += ["point", _ONES[tenth]]
    atoms.append("seconds")
    return atoms


def template_stems() -> list[tuple[str, int]]:
    """Fixed text fragments around the {slots} in rule say templates.

    Returns (stem, priority) pairs deduped by stem, sorted by priority."""
    root = Path(__file__).resolve().parents[1] / "config" / "defaults" / "rules"
    best: dict[str, int] = {}

    def add(text: str, priority: int) -> None:
        if "{" not in text:
            return
        for fragment in re.split(r"[\w']*\{[^{}]*\}[\w']*", text):
            stem = fragment.strip().lstrip(".,;:!?").lstrip()
            if sum(c.isalnum() for c in stem) < 2:
                continue
            if stem not in best or priority < best[stem]:
                best[stem] = priority

    for path in sorted(root.glob("*.yaml")):
        for rule in yaml.safe_load(path.read_text(encoding="utf-8")).get("rules", []):
            definition = RuleDefModel.model_validate(rule)
            priority = definition.priority
            for text in definition.say_pool():
                add(text, priority)
            for escalation in definition.escalate:
                for text in escalation.say:
                    add(text, priority)
            for severity in definition.severity:
                for text in severity.say:
                    add(text, severity.priority or priority)

    return sorted(best.items(), key=lambda item: (item[1], item[0]))


def slot_vocabulary() -> dict[str, list[str]]:
    return {
        "drivers": DRIVER_NAMES,
        "teams": TEAM_NAMES,
        "positions": POSITIONS,
        "temps": TEMPS,
        "lap_time_atoms": LAP_TIME_ATOMS,
        "digits": DIGITS,
    }
