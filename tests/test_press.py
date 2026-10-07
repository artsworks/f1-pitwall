from __future__ import annotations

import pytest

from pitwall.input.press import PressDetector


def test_single_press_acks_after_double_window() -> None:
    d = PressDetector()
    assert d.edge(0.0, True) is None
    assert d.edge(0.1, False) is None
    assert d.tick(0.3) is None
    p = d.tick(0.46)
    assert p is not None and p.kind == "ack"


def test_double_press_neg() -> None:
    d = PressDetector()
    d.edge(0.0, True)
    d.edge(0.1, False)
    p = d.edge(0.2, True)
    assert p is not None and p.kind == "neg"
    # third press still resolves as neg (no trailing ack)
    d.edge(0.3, False)
    d.edge(0.4, True)
    assert d.edge(0.5, False) is None
    assert d.tick(1.0) is None


def test_long_press_bookmark_release_ignored() -> None:
    d = PressDetector()
    d.edge(0.0, True)
    p = d.tick(0.85)
    assert p is not None and p.kind == "bookmark"
    assert d.edge(0.9, False) is None
    # next press starts clean
    assert d.edge(2.0, True) is None
    assert d.edge(2.1, False) is None
    p = d.tick(2.5)
    assert p is not None and p.kind == "ack"


def test_bounce_and_autorepeat_ignored() -> None:
    d = PressDetector()
    d.edge(0.0, True)
    # second down 20 ms later = bounce, not a double press
    assert d.edge(0.02, True) is None
    # auto-repeat downs while held are ignored
    assert d.edge(0.3, True) is None
    d.edge(0.4, False)
    p = d.tick(0.8)
    assert p is not None and p.kind == "ack"


def _recorded_presses(edges: list[tuple[float, bool]], tail_s: float = 0.4) -> list[str]:
    detector = PressDetector()
    presses: list[str] = []
    previous = edges[0][0]
    for t, down in edges:
        tick_t = previous + 0.01
        while tick_t < t:
            press = detector.tick(tick_t)
            if press is not None:
                presses.append(press.kind)
            tick_t += 0.01
        press = detector.edge(t, down)
        if press is not None:
            presses.append(press.kind)
        previous = t
    end = edges[-1][0] + tail_s
    tick_t = previous + 0.01
    while tick_t <= end:
        press = detector.tick(tick_t)
        if press is not None:
            presses.append(press.kind)
        tick_t += 0.01
    return presses


@pytest.mark.parametrize(
    ("case_id", "edges", "expected"),
    [
        pytest.param(
            "race double tap 191 ms",
            [(233.295, True), (233.363, False), (233.486, True), (233.553, False)],
            ["neg"],
            id="race double tap 191 ms",
        ),
        pytest.param(
            "real double tap 178 ms",
            [(395.804, True), (395.870, False), (395.982, True), (396.038, False)],
            ["neg"],
            id="real double tap 178 ms",
        ),
        pytest.param(
            "real double tap 214 ms",
            [(486.611, True), (486.701, False), (486.825, True), (486.894, False)],
            ["neg"],
            id="real double tap 214 ms",
        ),
        pytest.param(
            "real double tap 224 ms",
            [(102.585, True), (102.652, False), (102.809, True), (102.900, False)],
            ["neg"],
            id="real double tap 224 ms",
        ),
        pytest.param(
            "real hold 1144 ms",
            [(173.488, True), (174.632, False)],
            ["bookmark"],
        ),
        pytest.param(
            "real tap 123 ms",
            [(235.803, True), (235.926, False)],
            ["ack"],
        ),
    ],
)
def test_real_recorded_press_timings(
    case_id: str, edges: list[tuple[float, bool]], expected: list[str]
) -> None:
    assert case_id
    assert _recorded_presses(edges) == expected
