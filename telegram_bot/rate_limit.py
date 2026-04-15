"""
Per-chat rate limiter. In-memory dict — no persistence needed since the
window is tiny (seconds) and a bot restart clearing it is harmless.
"""
from __future__ import annotations

import time
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        window_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_s = float(window_s)
        self._clock = clock
        self._last: dict[str, float] = {}

    def check(self, chat_id: str) -> tuple[bool, float]:
        """
        Return (allowed, retry_after_s).
        - allowed=True: records the tick and lets the caller proceed.
        - allowed=False: does NOT update the tick; returns seconds the caller
          should wait before trying again.
        """
        now = self._clock()
        last = self._last.get(chat_id)
        if last is None or (now - last) >= self.window_s:
            self._last[chat_id] = now
            return True, 0.0
        retry_after = self.window_s - (now - last)
        return False, max(0.0, retry_after)
