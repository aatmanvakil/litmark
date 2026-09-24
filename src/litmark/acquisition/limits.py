"""Per-provider pacing.

The four providers differ by two orders of magnitude, so one global limit
would either hammer arXiv or crawl against Crossref. arXiv is the binding
constraint: its terms ask for one request every three seconds on a single
connection, which is a gate rather than a timeout.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

#: Verified September 2026 against each provider's published terms.
MIN_INTERVAL = {
    # "no more than one request every three seconds... a single connection"
    "arxiv": 3.0,
    # Polite pool: 10 per interval, concurrency 3. Comfortably paced.
    "crossref": 0.12,
    # Well under the documented 100/s ceiling; the real limit is a daily budget.
    "openalex": 0.05,
    # 100k a day; one lookup is nowhere near it.
    "unpaywall": 0.05,
}


@dataclass
class RateLimiter:
    """A minimum interval per provider, and a cooldown a provider asked for."""

    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic
    _last: dict[str, float] = field(default_factory=dict)
    _until: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self, provider: str) -> None:
        """Block until this provider may be called again."""
        with self._lock:
            previous = self._last.get(provider)
            # Only pace against a call actually made: defaulting the previous
            # timestamp to zero would delay the very first request by the
            # whole interval, which for arXiv is three seconds of nothing.
            earliest = self._until.get(provider, 0.0)
            if previous is not None:
                earliest = max(earliest, previous + MIN_INTERVAL.get(provider, 0.0))
            delay = earliest - self.now()
        if delay > 0:
            self.sleep(delay)
        with self._lock:
            self._last[provider] = self.now()

    def cooling_down(self, provider: str) -> float:
        """Seconds left on a cooldown, or 0. Checked before spending a call."""
        return max(0.0, self._until.get(provider, 0.0) - self.now())

    def back_off(self, provider: str, seconds: float | None) -> None:
        """Honour a Retry-After, so the next lookup does not pile on.

        A provider that said 429 is not retried within this lookup; this
        stops the *next* one from walking straight into the same wall.
        """
        # A provider that rate-limited us without saying for how long still
        # gets a pause; guessing short would be worse than guessing long.
        wait = 60.0 if seconds is None else max(seconds, 1.0)
        with self._lock:
            self._until[provider] = self.now() + wait
