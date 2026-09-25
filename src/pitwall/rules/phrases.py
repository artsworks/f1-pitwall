"""Phrase selection for rule calls.

A rule's `say` can be one template or a pool of variants, and `escalate` tiers
swap in a different pool once the same call has triggered N times inside
`repeat_window_s`. The first call of a pool is always its first variant (the
plain one); after that a shuffle bag walks every variant before any repeats and
never says the same line twice in a row. The bag is seeded by rule id, so a
replay picks the same words as the live session did.
"""

from __future__ import annotations

import random
import zlib
from collections import deque
from collections.abc import Sequence

from pitwall.config.models import RuleDefModel


class PhraseBook:
    def __init__(self, defn: RuleDefModel) -> None:
        self.defn = defn
        self._rng = random.Random(zlib.crc32(defn.id.encode()))
        self._bags: dict[int, list[int]] = {}
        self._last: dict[int, int] = {}
        self._triggers: deque[float] = deque()

    def trigger(self, now: float) -> int:
        """Record one trigger; return how many fell inside the repeat window."""
        self._triggers.append(now)
        while self._triggers and now - self._triggers[0] > self.defn.repeat_window_s:
            self._triggers.popleft()
        return len(self._triggers)

    def pool(self, repeat: int) -> tuple[int, Sequence[str]]:
        tier, pool = 0, self.defn.say_pool()
        for i, esc in enumerate(sorted(self.defn.escalate, key=lambda e: e.after), start=1):
            if repeat >= esc.after and esc.say:
                tier, pool = i, esc.say
        return tier, pool

    def pick(self, repeat: int) -> str:
        tier, pool = self.pool(repeat)
        if not pool:
            return ""
        if len(pool) == 1:
            return pool[0]
        if tier not in self._last:
            self._last[tier] = 0
            return pool[0]
        bag = self._bags.get(tier)
        if not bag:
            bag = list(range(len(pool)))
            self._rng.shuffle(bag)
            if bag[-1] == self._last[tier]:
                bag[0], bag[-1] = bag[-1], bag[0]
            self._bags[tier] = bag
        i = bag.pop()
        self._last[tier] = i
        return pool[i]
