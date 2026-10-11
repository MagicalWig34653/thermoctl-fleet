"""Shared virtual clock: one libfaketime timestamp file for every process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SIM_START = datetime(2026, 1, 5, 8, 0, 0, tzinfo=UTC)  # a Monday


CLOCK_DIRNAME = "clock"  # own directory: the fleet container mounts only this, read-only


def clock_path(state_dir: Path) -> Path:
    """The one place that defines where the shared clock file lives."""
    return state_dir / CLOCK_DIRNAME / "clock"


@dataclass
class SimClock:
    path: Path
    now: datetime = SIM_START

    def write(self) -> None:
        """Publish the current simulated time (readers: libfaketime, cache duration 1 s).

        The file is overwritten IN PLACE (fixed width, one write): replacing it by a
        rename makes libfaketime in the containers fall back to real time for a moment.
        """
        data = (self.now.strftime("%Y-%m-%d %H:%M:%S") + "\n").encode("ascii")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, self.path)  # first creation: nobody reads it yet
            return
        fd = os.open(self.path, os.O_WRONLY)
        try:
            os.pwrite(fd, data, 0)
        finally:
            os.close(fd)

    def advance(self, delta: timedelta) -> datetime:
        if delta <= timedelta(0):
            raise ValueError("the simulated clock only moves forward")
        self.now += delta
        self.write()
        return self.now

    def week_index(self) -> int:
        return (self.now - SIM_START).days // 7
