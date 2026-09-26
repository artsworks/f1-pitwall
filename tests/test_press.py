from __future__ import annotations

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
