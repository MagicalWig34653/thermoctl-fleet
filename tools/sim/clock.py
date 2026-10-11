"""Shared virtual clock: one libfaketime timestamp file for every process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SIM_START = datetime(2026, 1, 5, 8, 0, 0, tzinfo=UTC)  # a Monday


@dataclass
class SimClock:
    path: Path
    now: datetime = SIM_START

    def write(self) -> None:
        """Atomically publish the current simulated time (FAKETIME_NO_CACHE=1 readers)."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(self.now.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="ascii")
        os.replace(tmp, self.path)

    def advance(self, delta: timedelta) -> datetime:
        if delta <= timedelta(0):
            raise ValueError("the simulated clock only moves forward")
        self.now += delta
        self.write()
        return self.now

    def week_index(self) -> int:
        return (self.now - SIM_START).days // 7
