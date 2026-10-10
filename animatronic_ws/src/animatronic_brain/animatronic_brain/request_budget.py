"""Bounds cloud LLM request volume (and therefore cost) with a daily
cap and a lifetime cap. -1 means unlimited, matching the -1 convention
already used for Ollama's keep_alive elsewhere in this project.
"""

from __future__ import annotations

import datetime


class RequestBudget:
    def __init__(self, daily_limit: int = -1, total_limit: int = -1, today_fn=None):
        self._daily_limit = daily_limit
        self._total_limit = total_limit
        self._today_fn = today_fn or (lambda: datetime.date.today())
        self._current_day = self._today_fn()
        self._daily_count = 0
        self._total_count = 0

    def _roll_day_if_needed(self):
        today = self._today_fn()
        if today != self._current_day:
            self._current_day = today
            self._daily_count = 0

    def try_consume(self) -> bool:
        """Returns True and records one request if under both caps;
        returns False (consuming nothing) if either cap is exhausted."""
        self._roll_day_if_needed()

        if self._daily_limit != -1 and self._daily_count >= self._daily_limit:
            return False
        if self._total_limit != -1 and self._total_count >= self._total_limit:
            return False

        self._daily_count += 1
        self._total_count += 1
        return True

    @property
    def daily_count(self) -> int:
        self._roll_day_if_needed()
        return self._daily_count

    @property
    def total_count(self) -> int:
        return self._total_count

    @property
    def daily_limit(self) -> int:
        return self._daily_limit

    @property
    def total_limit(self) -> int:
        return self._total_limit
