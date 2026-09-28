from __future__ import annotations

from pitwall.config.models import RuleDefModel
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import Snapshot


def _engine() -> RuleEngine:
    rule = RuleDefModel.model_validate(
        {
            "id": "gap",
            "priority": 3,
            "when": "gap_behind_s < 1.0",
            "say": ["Car behind, {gap_behind_s:.1f}"],
            "severity": [
                {"when": "gap_behind_s < 0.3", "priority": 1, "say": ["He's on you!"]},
            ],
        }
    )
    return RuleEngine([rule], thresholds={}, mode={})


def test_severity_tier_swaps_phrase_and_priority() -> None:
    e = _engine()
    (c,) = e.evaluate(Snapshot(now=0.0, gap_behind_s=0.8)).candidates
    assert (c.text, c.priority) == ("Car behind, 0.8", 3)
    e.evaluate(Snapshot(now=1.0, gap_behind_s=5.0))
    (c,) = e.evaluate(Snapshot(now=2.0, gap_behind_s=0.2)).candidates
    assert (c.text, c.priority) == ("He's on you!", 1)
