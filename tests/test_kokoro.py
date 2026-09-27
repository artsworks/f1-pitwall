import io
import wave
from pathlib import Path

import pytest

from pitwall.audio.kokoro_tts import kokoro_installed, make_kokoro_tone_synths
from pitwall.audio.speaker import NullSpeaker, make_speaker
from pitwall.config.models import SpeechSettings


def test_missing_kokoro_model_falls_back(tmp_path: Path) -> None:
    s = SpeechSettings(engine="kokoro", voices_dir=str(tmp_path))
    assert not kokoro_installed(s)
    with pytest.raises(FileNotFoundError, match="pitwall voices kokoro"):
        make_kokoro_tone_synths(s)
    assert isinstance(make_speaker(s), NullSpeaker)


VOICES = Path.home() / "voices"
REAL = SpeechSettings(voices_dir=str(VOICES), radio_click=False)


@pytest.mark.skipif(not kokoro_installed(REAL), reason="no kokoro model")
def test_real_kokoro_renders_each_priority_at_its_pace() -> None:
    synths = make_kokoro_tone_synths(REAL)
    seconds = {}
    for prio, synth in synths.items():
        wav, seconds[prio] = synth("Box box, box box. Plan A, hards")
        with wave.open(io.BytesIO(wav)) as w:
            assert w.getframerate() == 24000 and w.getnframes() > 24000 * 0.5
    assert seconds[1] < seconds[2] < seconds[3]


def test_unknown_kokoro_voice_rejected() -> None:
    if not kokoro_installed(REAL):
        pytest.skip("no kokoro model")
    with pytest.raises(ValueError, match="not in"):
        make_kokoro_tone_synths(REAL.model_copy(update={"kokoro_voice": "nope"}))
