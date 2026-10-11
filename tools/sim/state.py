"""Atomic checkpoint of the simulation, written after every persona step."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class SimState:
    week: int = 0
    done_steps: list[str] = field(default_factory=list)
    applied_events: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> SimState:
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def mark_step(self, step_id: str) -> None:
        if step_id not in self.done_steps:
            self.done_steps.append(step_id)

    def is_done(self, step_id: str) -> bool:
        return step_id in self.done_steps

    def mark_event(self, event_id: str) -> None:
        if event_id not in self.applied_events:
            self.applied_events.append(event_id)
