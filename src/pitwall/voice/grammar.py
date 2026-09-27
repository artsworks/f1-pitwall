"""Closed voice grammar: intent -> phrases, rendered as W3C SRGS XML for SAPI.

The recogniser returns the matched phrase text, so the intent is a dictionary
lookup; SRGS semantic tags are not needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

SRGS_NS = "http://www.w3.org/2001/06/grammar"
ROOT_RULE = "request"

_NON_WORD = re.compile(r"[^a-z0-9' ]+")
_SPACES = re.compile(r"\s+")

# SAPI recogniser token "Language" attribute (hex LCID) -> xml:lang
LCID_LANG = {
    "409": "en-US",
    "809": "en-GB",
    "c09": "en-AU",
    "1009": "en-CA",
    "4009": "en-IN",
}


def normalise(text: str) -> str:
    t = text.lower().replace("\u2019", "'")
    t = _NON_WORD.sub(" ", t)
    return _SPACES.sub(" ", t).strip()


def lang_for_lcid(attr: str) -> str:
    """'809' or '809;9' -> 'en-GB'; unknown -> 'en-US'."""
    first = attr.split(";", 1)[0].strip().lower()
    return LCID_LANG.get(first, "en-US")


@dataclass(frozen=True)
class VoiceGrammar:
    intents: dict[str, tuple[str, ...]]

    @classmethod
    def from_mapping(cls, intents: dict[str, list[str]]) -> VoiceGrammar:
        seen: dict[str, str] = {}
        out: dict[str, tuple[str, ...]] = {}
        for intent, phrases in intents.items():
            if not phrases:
                raise ValueError(f"voice intent {intent!r} has no phrases")
            norm: list[str] = []
            for p in phrases:
                n = normalise(p)
                if not n:
                    raise ValueError(f"voice intent {intent!r} has an empty phrase")
                if n in seen:
                    raise ValueError(f"voice phrase {p!r} is in both {seen[n]!r} and {intent!r}")
                seen[n] = intent
                norm.append(n)
            out[intent] = tuple(norm)
        if not out:
            raise ValueError("voice grammar has no intents")
        return cls(out)

    @property
    def phrases(self) -> list[str]:
        return [p for ps in self.intents.values() for p in ps]

    def intent_for(self, text: str) -> str | None:
        n = normalise(text)
        for intent, phrases in self.intents.items():
            if n in phrases:
                return intent
        return None

    def to_srgs(self, lang: str = "en-US") -> str:
        items = "\n".join(f"      <item>{escape(p)}</item>" for p in self.phrases)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<grammar version="1.0" xml:lang="{escape(lang)}" root="{ROOT_RULE}" '
            f'mode="voice" xmlns="{SRGS_NS}">\n'
            f'  <rule id="{ROOT_RULE}" scope="public">\n'
            "    <one-of>\n"
            f"{items}\n"
            "    </one-of>\n"
            "  </rule>\n"
            "</grammar>\n"
        )
