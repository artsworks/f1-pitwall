from __future__ import annotations

import re

from pitwall.audio.slots import (
    POSITIONS,
    TEMPS,
    lap_time_atoms,
    slot_vocabulary,
    template_stems,
)


def test_number_words_in_temps() -> None:
    assert "seventy-two" in TEMPS
    assert "one hundred" in TEMPS
    assert "one oh five" in TEMPS
    assert len(TEMPS) == 36


def test_positions() -> None:
    assert POSITIONS[0] == "P1"
    assert POSITIONS[-1] == "P22"
    assert len(POSITIONS) == 22


def test_lap_time_atoms() -> None:
    assert lap_time_atoms(79_200) == ["one minute", "nineteen", "point", "two", "seconds"]
    assert lap_time_atoms(80_000) == ["one minute", "twenty", "seconds"]


def test_template_stems_are_fixed_fragments() -> None:
    stems = template_stems()
    assert stems
    assert all("{" not in text and "}" not in text for text, _ in stems)
    assert all(sum(c.isalnum() for c in text) >= 2 for text, _ in stems)
    assert all(priority in (1, 2, 3) for _, priority in stems)
    assert all(not text.startswith(tuple(".,;:!?")) for text, _ in stems)
    assert all(not re.search(r"\bP$", text) for text, _ in stems)


def test_slot_vocabulary_keys() -> None:
    vocab = slot_vocabulary()
    assert set(vocab) == {
        "drivers",
        "teams",
        "positions",
        "temps",
        "lap_time_atoms",
        "digits",
    }
    assert all(vocab[key] for key in vocab)
