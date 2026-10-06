"""Startup PIN gate for dashboard clients."""

from __future__ import annotations

import hmac
import ipaddress
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

PIN_COOKIE = "pitwall_pin"


@dataclass(frozen=True)
class PinResult:
    ok: bool
    tries_left: int
    retry_after_s: float


@dataclass
class _HostAttempts:
    fails: int = 0
    locked_until: float = 0.0


class PinGate:
    def __init__(
        self,
        pin: str | None = None,
        *,
        max_tries: int = 5,
        lockout_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_tries < 1:
            raise ValueError("max_tries must be positive")
        if lockout_s <= 0:
            raise ValueError("lockout_s must be positive")
        if pin is not None and (len(pin) != 4 or not pin.isascii() or not pin.isdigit()):
            raise ValueError("pin must be four ASCII digits")
        self.pin = pin if pin is not None else f"{secrets.randbelow(10000):04d}"
        self.token = secrets.token_urlsafe(32)
        self.max_tries = max_tries
        self.lockout_s = lockout_s
        self._clock = clock
        self._attempts: dict[str, _HostAttempts] = {}
        self._lock = threading.Lock()

    def allowed(self, host: str | None, cookie: str | None) -> bool:
        if host is not None:
            try:
                if ipaddress.ip_address(host).is_loopback:
                    return True
            except ValueError:
                pass
        return hmac.compare_digest((cookie or "").encode("utf-8"), self.token.encode("ascii"))

    def check(self, host: str, pin: str) -> PinResult:
        with self._lock:
            state = self._attempts.setdefault(host, _HostAttempts())
            now = self._clock()
            if state.locked_until > now:
                return PinResult(False, 0, state.locked_until - now)
            if state.locked_until:
                state.locked_until = 0.0
                state.fails = 0

            if hmac.compare_digest(pin.encode("utf-8"), self.pin.encode("ascii")):
                state.fails = 0
                return PinResult(True, self.max_tries, 0.0)

            state.fails += 1
            if state.fails >= self.max_tries:
                state.fails = 0
                state.locked_until = now + self.lockout_s
                return PinResult(False, 0, self.lockout_s)
            return PinResult(False, self.max_tries - state.fails, 0.0)
