"""Atomic checkpoint of the simulation, written after every persona step."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class SimState:
    week: int = 0
    done_steps: list[str] = field(default_factory=list)
    applied_events: list[str] = field(default_factory=list)
    # Run parameters that must not change on resume; None = not recorded (older state).
    personas: list[str] | None = None
    seed: int | None = None

    @classmethod
    def load(cls, path: Path) -> SimState:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt simulation state file {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"corrupt simulation state file {path}: expected a JSON object")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def has_progress(self) -> bool:
        return self.week > 0 or bool(self.done_steps) or bool(self.applied_events)

    def mark_step(self, step_id: str) -> None:
        if step_id not in self.done_steps:
            self.done_steps.append(step_id)

    def is_done(self, step_id: str) -> bool:
        return step_id in self.done_steps

    def mark_event(self, event_id: str) -> None:
        if event_id not in self.applied_events:
            self.applied_events.append(event_id)
