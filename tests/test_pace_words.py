import math

import pytest

from pitwall.strategy.pace import pace_words


@pytest.mark.parametrize(
    ("d", "words"),
    [
        (0.01, "on the same pace"),
        (0.04, "4 hundredths faster"),
        (0.12, "a tenth faster"),
        (-0.31, "three tenths slower"),
        (0.5, "half a second faster"),
        (1.0, "a second faster"),
        (-1.42, "1.4 seconds slower"),
        (math.inf, ""),
    ],
)
def test_pace_words(d: float, words: str) -> None:
    assert pace_words(d) == words
