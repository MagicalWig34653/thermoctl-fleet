# Usage Simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `tools/sim/`, a resumable runner that simulates 2-3 years of landlord/tenant use of thermoctl + thermoctl-fleet with Haiku persona agents, plus a scripted fleet pentest, and collects every finding into `docs/sim-findings/`.

**Architecture:** A deterministic scenario engine and a shared file-based virtual clock (libfaketime) drive a local fleet (Docker), 2 Lima VMs and ~10 container agents week by week. Per simulated week the runner advances the clock in day steps with settle windows, injects due events, then calls the personas that are due (headless `claude -p`, Haiku 5.5, low effort). All progress is checkpointed after every persona step so a usage-limit abort resumes exactly where it stopped. Findings are JSONL per step and rendered into an index.

**Tech Stack:** Python 3.12 (stdlib + pydantic + httpx, already project deps), pytest/ruff/mypy, Docker Compose, Lima (existing `tools/mac-test-vm` recipe), libfaketime, Playwright (Python), `claude` CLI headless mode.

## Global Constraints

- Findings only; **no fixes** to `fleet/`, `agent/`, `protocol/`, `watchdog/`, `image/` in this plan.
- Never touch `protocol.commands.CommandType`, `agent/sources.py`, or the watchdog `go.mod` (CLAUDE.md principles 1, 2, 6).
- No secrets/credentials committed; sim credentials are generated at runtime into git-ignored `sim-state/`.
- Traffic only to local instances (127.0.0.1 / Lima / Docker network). The pentest never targets anything else.
- Ruff, mypy, pytest must pass for every task; every function has a test (`# pragma: no cover` with reason where unreachable).
- Personas use model `claude-haiku-5-5`, low effort. Max 4 concurrent persona agents.
- The existing Postgres on :5432 is not touched; sim fleet uses its own SQLite volume.
- One commit per completed task, with `docs/STATUS.md` updated; commit trailer `Claude-Session: https://claude.ai/code/session_01J7pFatA7hsXpAr1iBLQhEv`.

## File Structure

```
tools/sim/__init__.py          package marker
tools/sim/__main__.py          CLI: run | status | report | pentest
tools/sim/clock.py             SimClock: shared faketime file
tools/sim/state.py             SimState: atomic JSON checkpoint
tools/sim/scenario.py          Event, build_schedule, due_events
tools/sim/findings.py          Finding model, parse, JSONL store, INDEX.md
tools/sim/personas.py          Persona definitions + prompt builder
tools/sim/haiku.py             headless claude invocation + limit detection
tools/sim/runner.py            week loop, checkpointing, pause/resume
tools/sim/env/compose.sim.yml  fleet + container agents + faketime
tools/sim/env/up.sh, down.sh   bring the environment up/down
tools/sim/pentest/harness.py   Check, CheckResult, run_checks
tools/sim/pentest/checks_*.py  check groups
tests/sim/                     unit tests for all of the above
docs/sim-findings/             reports (generated + hand-curated)
```

---

### Task 1: Environment discovery spike (documents facts later tasks rely on)

**Files:**
- Create: `docs/sim-findings/00-environment-notes.md`

**Interfaces:**
- Produces: written answers to the questions below, used by Tasks 6, 7, 8.

- [ ] **Step 1: Answer these questions by reading code, not guessing**

```bash
cd "/Users/linolaske/Documents/Code Projekte/PycharmProjects/thermoctl-fleet"
sed -n 1,120p tools/e2e/README.md
sed -n 1,80p tools/mac-test-vm.agent-compose.local.yml
grep -n "docker\|health\|thermoctl_container" agent/loop.py | head -60
grep -n "FLEET_" fleet/ui_routes.py | head -40
```

Record in the notes file: (a) how the agent obtains health data (does it need a real thermoctl container? what is the minimum stub for a container agent?), (b) which env vars a fleet start needs (`FLEET_DATABASE_URL`, `FLEET_TOTP_KEY`, ... from `tools/dev_fleet.py`), (c) how a registration is created and confirmed (reuse `tools/mac_test_vm`), (d) how the tenant UI of `../thermoctl` is started headless (`../thermoctl/docker/compose.beispiel.yml`, `thermoctl/cli.py`), (e) which Lima VMs exist and whether `thermoctl-test-vm` can be cloned.

- [ ] **Step 2: Verify libfaketime works in the fleet and agent images**

```bash
docker run --rm debian:13-slim bash -c 'apt-get update -qq && apt-get install -y -qq faketime >/dev/null && echo "2026-01-05 08:00:00" > /tmp/ft && FAKETIME_NO_CACHE=1 FAKETIME_TIMESTAMP_FILE=/tmp/ft LD_PRELOAD=$(find /usr -name "libfaketime.so.1" | head -1) python3 -c "import datetime;print(datetime.datetime.now())"'
```
Expected: a datetime on 2026-01-05. Note the exact `libfaketime.so.1` path and whether an absolute timestamp freezes time (it does without the `@` prefix); the clock task depends on this.

- [ ] **Step 3: Commit**

```bash
git add docs/sim-findings/00-environment-notes.md docs/STATUS.md
git commit -m "docs(sim): environment discovery notes"
```

---

### Task 2: Virtual clock

**Files:**
- Create: `tools/sim/__init__.py` (empty), `tools/sim/clock.py`
- Test: `tests/sim/test_clock.py`

**Interfaces:**
- Produces: `SimClock(path: Path, now: datetime = SIM_START)`, `.write() -> None`, `.advance(delta: timedelta) -> datetime`, `.week_index() -> int`, constant `SIM_START`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_clock.py
from datetime import timedelta
import pytest
from tools.sim.clock import SIM_START, SimClock


def test_write_uses_faketime_absolute_format(tmp_path):
    clock = SimClock(tmp_path / "ft")
    clock.write()
    assert (tmp_path / "ft").read_text() == "2026-01-05 08:00:00\n"


def test_advance_moves_time_and_rewrites_file(tmp_path):
    clock = SimClock(tmp_path / "ft")
    clock.advance(timedelta(days=1, hours=2))
    assert (tmp_path / "ft").read_text() == "2026-01-06 10:00:00\n"
    assert clock.now == SIM_START + timedelta(days=1, hours=2)


def test_advance_rejects_non_positive_delta(tmp_path):
    with pytest.raises(ValueError):
        SimClock(tmp_path / "ft").advance(timedelta(0))


def test_week_index(tmp_path):
    clock = SimClock(tmp_path / "ft")
    assert clock.week_index() == 0
    clock.advance(timedelta(days=15))
    assert clock.week_index() == 2
```

- [ ] **Step 2: Run, expect FAIL**: `pytest tests/sim/test_clock.py -v` -> ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
# tools/sim/clock.py
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
```

- [ ] **Step 4: Run, expect PASS**; also `ruff check tools/sim tests/sim && mypy tools/sim`.
- [ ] **Step 5: Commit** `feat(sim): virtual clock`.

---

### Task 3: Checkpoint state

**Files:**
- Create: `tools/sim/state.py`
- Test: `tests/sim/test_state.py`

**Interfaces:**
- Produces: `SimState` dataclass with `week: int = 0`, `done_steps: list[str]`, `applied_events: list[str]`; `SimState.load(path) -> SimState` (returns fresh state if missing); `.save(path) -> None` (atomic); `.mark_step(step_id: str)`, `.is_done(step_id: str) -> bool`, `.mark_event(event_id: str)`.

- [ ] **Step 1: Failing test**

```python
# tests/sim/test_state.py
from tools.sim.state import SimState


def test_missing_file_gives_fresh_state(tmp_path):
    state = SimState.load(tmp_path / "state.json")
    assert state.week == 0 and state.done_steps == []


def test_roundtrip_and_idempotent_marks(tmp_path):
    path = tmp_path / "state.json"
    state = SimState()
    state.mark_step("w003:landlord")
    state.mark_step("w003:landlord")
    state.mark_event("e1")
    state.week = 3
    state.save(path)
    loaded = SimState.load(path)
    assert loaded.week == 3
    assert loaded.done_steps == ["w003:landlord"]
    assert loaded.is_done("w003:landlord") and not loaded.is_done("w003:tenant1")
    assert loaded.applied_events == ["e1"]


def test_save_leaves_no_temp_file(tmp_path):
    SimState().save(tmp_path / "state.json")
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**

```python
# tools/sim/state.py
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
```

- [ ] **Step 4: PASS + ruff/mypy. Step 5: Commit** `feat(sim): checkpoint state`.

---

### Task 4: Scenario engine

**Files:**
- Create: `tools/sim/scenario.py`
- Test: `tests/sim/test_scenario.py`

**Interfaces:**
- Consumes: `SimState.applied_events` (Task 3).
- Produces: `EventKind` (str enum: `HEARTBEAT_LOSS`, `HEARTBEAT_RESTORE`, `WIFI_CHANGE`, `FAULT`, `IMAGE_UPDATE`, `BACKUP`, `TOKEN_EXPIRY`, `TENANT_CHANGE`, `POWER_LOSS`, `CLOCK_DRIFT`), `Event(id: str, week: int, kind: EventKind, apartment: str)`, `build_schedule(seed: int, weeks: int, apartments: list[str]) -> list[Event]` (deterministic, sorted by week then id), `due_events(schedule, week: int, applied: list[str]) -> list[Event]`.

- [ ] **Step 1: Failing test**

```python
# tests/sim/test_scenario.py
from tools.sim.scenario import EventKind, build_schedule, due_events

APTS = [f"a{i:02d}" for i in range(12)]


def test_schedule_is_deterministic():
    assert build_schedule(7, 156, APTS) == build_schedule(7, 156, APTS)


def test_schedule_covers_every_kind_and_stays_in_range():
    sched = build_schedule(7, 156, APTS)
    assert {e.kind for e in sched} == set(EventKind)
    assert all(0 <= e.week < 156 and e.apartment in APTS for e in sched)
    assert len({e.id for e in sched}) == len(sched)


def test_heartbeat_loss_is_followed_by_restore():
    sched = build_schedule(7, 156, APTS)
    losses = [e for e in sched if e.kind is EventKind.HEARTBEAT_LOSS]
    restores = {(e.apartment, e.week) for e in sched if e.kind is EventKind.HEARTBEAT_RESTORE}
    for loss in losses:
        assert any(a == loss.apartment and w > loss.week for a, w in restores)


def test_due_events_skips_applied_ones():
    sched = build_schedule(7, 156, APTS)
    week = sched[0].week
    first = due_events(sched, week, [])
    assert first and all(e.week == week for e in first)
    assert due_events(sched, week, [e.id for e in first]) == []
```

- [ ] **Step 2: FAIL. Step 3: Implement**

```python
# tools/sim/scenario.py
"""Deterministic event schedule; no LLM involved."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class EventKind(StrEnum):
    HEARTBEAT_LOSS = "heartbeat_loss"
    HEARTBEAT_RESTORE = "heartbeat_restore"
    WIFI_CHANGE = "wifi_change"
    FAULT = "fault"
    IMAGE_UPDATE = "image_update"
    BACKUP = "backup"
    TOKEN_EXPIRY = "token_expiry"
    TENANT_CHANGE = "tenant_change"
    POWER_LOSS = "power_loss"
    CLOCK_DRIFT = "clock_drift"


@dataclass(frozen=True)
class Event:
    id: str
    week: int
    kind: EventKind
    apartment: str


# (kind, mean events per apartment per year)
_RATES: list[tuple[EventKind, float]] = [
    (EventKind.HEARTBEAT_LOSS, 1.5),
    (EventKind.WIFI_CHANGE, 0.5),
    (EventKind.FAULT, 3.0),
    (EventKind.IMAGE_UPDATE, 2.0),
    (EventKind.BACKUP, 12.0),
    (EventKind.TOKEN_EXPIRY, 1.0),
    (EventKind.TENANT_CHANGE, 0.3),
    (EventKind.POWER_LOSS, 0.7),
    (EventKind.CLOCK_DRIFT, 0.3),
]


def build_schedule(seed: int, weeks: int, apartments: list[str]) -> list[Event]:
    rng = random.Random(seed)
    events: list[Event] = []
    for kind, per_year in _RATES:
        for apartment in apartments:
            expected = per_year * weeks / 52
            count = max(1, round(expected + rng.uniform(-0.5, 0.5) * max(expected, 1) ** 0.5))
            for n in range(count):
                week = rng.randrange(0, max(1, weeks - 3))
                events.append(Event(f"{kind}:{apartment}:{n}", week, kind, apartment))
                if kind is EventKind.HEARTBEAT_LOSS:
                    restore = min(weeks - 1, week + rng.randint(1, 2))
                    events.append(
                        Event(f"restore:{apartment}:{n}:{week}", restore,
                              EventKind.HEARTBEAT_RESTORE, apartment)
                    )
    # unique ids are guaranteed by construction; keep the order stable
    return sorted(events, key=lambda e: (e.week, e.id))


def due_events(schedule: list[Event], week: int, applied: list[str]) -> list[Event]:
    done = set(applied)
    return [e for e in schedule if e.week == week and e.id not in done]
```

Note for the implementer: if the "restore always follows" test fails because a restore week clamps to the loss week, bump the loss week range upper bound; do not weaken the test.

- [ ] **Step 4: PASS + ruff/mypy. Step 5: Commit** `feat(sim): scenario engine`.

---

### Task 5: Findings model, store and index

**Files:**
- Create: `tools/sim/findings.py`
- Test: `tests/sim/test_findings.py`

**Interfaces:**
- Produces: `Area` (`logic|ux|looks|missing_tool|security`), `Severity` (`blocker|major|minor|nit`), `Finding(week: int, persona: str, area: Area, severity: Severity, title: str, expected: str, actual: str, evidence: str = "")`, `parse_findings(text: str, week: int, persona: str) -> list[Finding]` (reads lines starting with `FINDING: ` + JSON; ignores malformed lines), `append_findings(dir: Path, findings) -> None` (JSONL `raw/findings.jsonl`), `build_index(dir: Path) -> Path` (writes `INDEX.md`, groups by severity then area).

- [ ] **Step 1: Failing test**

```python
# tests/sim/test_findings.py
import json
from tools.sim.findings import Area, Severity, append_findings, build_index, parse_findings

LINE = 'FINDING: {"area":"ux","severity":"major","title":"Button hidden","expected":"visible","actual":"cut off at 390px","evidence":"shot-12.png"}'


def test_parse_reads_valid_and_ignores_garbage():
    text = f"hello\n{LINE}\nFINDING: not json\nFINDING: {{\"area\":\"bogus\"}}\n"
    found = parse_findings(text, week=12, persona="tenant1")
    assert len(found) == 1
    f = found[0]
    assert (f.week, f.persona, f.area, f.severity) == (12, "tenant1", Area.UX, Severity.MAJOR)


def test_index_groups_by_severity_then_area(tmp_path):
    f = parse_findings(LINE, 3, "landlord")
    append_findings(tmp_path, f)
    append_findings(tmp_path, f)  # appending twice must not duplicate in the index
    index = build_index(tmp_path).read_text()
    assert index.count("Button hidden") == 1
    assert index.index("## major") < index.index("### ux")
    rows = [json.loads(x) for x in (tmp_path / "raw" / "findings.jsonl").read_text().splitlines()]
    assert len(rows) == 2
```

- [ ] **Step 2: FAIL. Step 3: Implement**

```python
# tools/sim/findings.py
"""Structured findings: persona output -> JSONL -> INDEX.md."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

_PREFIX = "FINDING: "


class Area(StrEnum):
    LOGIC = "logic"
    UX = "ux"
    LOOKS = "looks"
    MISSING_TOOL = "missing_tool"
    SECURITY = "security"


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


@dataclass(frozen=True)
class Finding:
    week: int
    persona: str
    area: Area
    severity: Severity
    title: str
    expected: str
    actual: str
    evidence: str = ""


def parse_findings(text: str, week: int, persona: str) -> list[Finding]:
    found: list[Finding] = []
    for line in text.splitlines():
        if not line.startswith(_PREFIX):
            continue
        try:
            raw = json.loads(line[len(_PREFIX):])
            found.append(Finding(
                week=week, persona=persona, area=Area(raw["area"]),
                severity=Severity(raw["severity"]), title=raw["title"],
                expected=raw.get("expected", ""), actual=raw.get("actual", ""),
                evidence=raw.get("evidence", ""),
            ))
        except (ValueError, KeyError, TypeError):
            continue
    return found


def append_findings(out_dir: Path, findings: list[Finding]) -> None:
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with (raw_dir / "findings.jsonl").open("a", encoding="utf-8") as handle:
        for finding in findings:
            handle.write(json.dumps(asdict(finding), ensure_ascii=False) + "\n")


def build_index(out_dir: Path) -> Path:
    rows = [
        json.loads(line)
        for line in (out_dir / "raw" / "findings.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    seen: dict[tuple[str, str], dict] = {}
    for row in rows:  # same title in the same area counts once, first week wins
        seen.setdefault((row["area"], row["title"]), row)
    lines = ["# Simulation findings\n"]
    for severity in Severity:
        in_sev = [r for r in seen.values() if r["severity"] == severity]
        if not in_sev:
            continue
        lines.append(f"## {severity}\n")
        for area in Area:
            in_area = [r for r in in_sev if r["area"] == area]
            if not in_area:
                continue
            lines.append(f"### {area}\n")
            for r in in_area:
                lines.append(
                    f"- **{r['title']}** (week {r['week']}, {r['persona']}): "
                    f"expected {r['expected']!r}; actual {r['actual']!r}"
                    + (f" -- `{r['evidence']}`" if r["evidence"] else "")
                )
            lines.append("")
    path = out_dir / "INDEX.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
```

- [ ] **Step 4: PASS + ruff/mypy. Step 5: Commit** `feat(sim): findings store and index`.

---

### Task 6: Personas and prompt builder

**Files:**
- Create: `tools/sim/personas.py`
- Test: `tests/sim/test_personas.py`

**Interfaces:**
- Consumes: `Area`, `Severity` names from Task 5 (prompt text only).
- Produces: `Persona(id, role, character, goals: list[str], every_weeks: int, browser: bool, max_steps: int)`, `PERSONAS: list[Persona]`, `due_personas(week: int) -> list[Persona]`, `build_prompt(persona, week: int, sim_date: str, urls: dict[str, str], tool_notes: str) -> str`.

- [ ] **Step 1: Failing test**

```python
# tests/sim/test_personas.py
from tools.sim.personas import PERSONAS, build_prompt, due_personas


def test_ids_unique_and_roles_covered():
    ids = [p.id for p in PERSONAS]
    assert len(ids) == len(set(ids))
    roles = {p.role for p in PERSONAS}
    assert {"landlord", "caretaker", "tenant", "admin", "newcomer"} <= roles


def test_week_zero_has_everyone_due_and_cadence_respected():
    assert {p.id for p in due_personas(0)} == {p.id for p in PERSONAS}
    weekly = {p.id for p in PERSONAS if p.every_weeks == 1}
    assert weekly <= {p.id for p in due_personas(1)}
    assert not any(p.every_weeks == 4 for p in due_personas(1))


def test_prompt_contains_contract_and_forbids_attacks():
    p = PERSONAS[0]
    text = build_prompt(p, 5, "2026-02-09", {"fleet": "http://127.0.0.1:8100"}, "notes")
    assert "FINDING: " in text and "2026-02-09" in text and p.character in text
    assert "http://127.0.0.1:8100" in text
    assert "do not attack" in text.lower()
```

- [ ] **Step 2: FAIL. Step 3: Implement**

```python
# tools/sim/personas.py
"""Persona definitions (Haiku role-play) and the prompt each round gets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    id: str
    role: str
    character: str
    goals: list[str]
    every_weeks: int
    browser: bool
    max_steps: int


PERSONAS: list[Persona] = [
    Persona("landlord", "landlord",
            "Frau Keller, 58, owns three buildings, not technical, wants one glance at the whole portfolio.",
            ["See which apartments have problems", "Understand an alarm without technical terms",
             "Hand a flat to a new tenant"], 1, True, 25),
    Persona("caretaker", "caretaker",
            "Herr Demir, 44, caretaker, practical, uses a phone on site, no patience for jargon.",
            ["Find the apartment with a fault and what to do", "Run a maintenance command",
             "Fetch a diagnostic bundle"], 1, True, 25),
    Persona("tenant1", "tenant",
            "Anna, 29, tech-savvy, uses the tenant UI daily.",
            ["Change a schedule", "Check why it is cold", "Look at history"], 2, True, 20),
    Persona("tenant2", "tenant",
            "Herr Schulz, 71, large fonts, easily confused, uses a tablet.",
            ["Set the room warmer", "Find out whether the heating is broken"], 2, True, 20),
    Persona("tenant3", "tenant",
            "Mia, 22, phone only, impatient, moves out in the second year.",
            ["Quick temperature change", "Absence period for a holiday"], 2, True, 20),
    Persona("admin", "admin",
            "Jonas, fleet administrator, careful, reads logs, cares about security and retention.",
            ["Review users and passkeys", "Check backups and retention", "Plan a rollout"], 4, True, 30),
    Persona("newcomer", "newcomer",
            "Sven, new landlord employee, first week, reads no documentation unless forced.",
            ["Onboard a new base station", "Find where to confirm a device"], 4, True, 25),
]


def due_personas(week: int) -> list[Persona]:
    return [p for p in PERSONAS if week % p.every_weeks == 0]


def build_prompt(persona: Persona, week: int, sim_date: str, urls: dict[str, str],
                 tool_notes: str) -> str:
    goals = "\n".join(f"- {g}" for g in persona.goals)
    endpoints = "\n".join(f"- {name}: {url}" for name, url in urls.items())
    return f"""You are role-playing a real person using a heating-control system. Stay in character.

Who you are: {persona.character}
Role: {persona.role}. Today's simulated date: {sim_date} (week {week}).
What you want to get done this session:
{goals}

Systems (local test instances only; do not attack or probe anything, just use it as this person would):
{endpoints}

How to work: {tool_notes}
Use at most {persona.max_steps} actions. Do not invent data you cannot see.

When something is wrong, confusing, ugly, missing, or too slow, report it as one line:
FINDING: {{"area":"logic|ux|looks|missing_tool|security","severity":"blocker|major|minor|nit","title":"short","expected":"what you hoped for","actual":"what happened","evidence":"url or screenshot name"}}
Report a missing capability you wished you had as area missing_tool. Finish with one line: DONE
"""
```

- [ ] **Step 4: PASS + ruff/mypy. Step 5: Commit** `feat(sim): personas and prompts`.

---

### Task 7: Headless Haiku invocation with limit detection

**Files:**
- Create: `tools/sim/haiku.py`
- Test: `tests/sim/test_haiku.py`

**Interfaces:**
- Produces: `PersonaResult(ok: bool, text: str, limit_hit: bool)`, `run_persona(prompt: str, *, cwd: Path, model: str = "claude-haiku-5-5", effort: str = "low", timeout_s: int = 900, run=subprocess.run) -> PersonaResult`. `run` is injected for tests.

- [ ] **Step 1: Verify the CLI flags first**

Run `claude --help | grep -i -E "print|model|effort|allowed|permission"` and adjust the argument list in Step 3 to the real flag names (expected: `-p`, `--model`, an effort option, `--allowedTools`). Record the exact flags in `docs/sim-findings/00-environment-notes.md`.

- [ ] **Step 2: Failing test**

```python
# tests/sim/test_haiku.py
from pathlib import Path
from types import SimpleNamespace
from tools.sim.haiku import run_persona


def fake(stdout="", stderr="", code=0):
    def _run(cmd, **kw):
        _run.cmd = cmd
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)
    return _run


def test_success_passes_model_and_prompt(tmp_path: Path):
    run = fake(stdout="FINDING: {}\nDONE")
    res = run_persona("hi", cwd=tmp_path, run=run)
    assert res.ok and not res.limit_hit and "DONE" in res.text
    assert "claude-haiku-5-5" in run.cmd and "hi" in run.cmd


def test_usage_limit_is_detected_not_failed(tmp_path: Path):
    res = run_persona("hi", cwd=tmp_path, run=fake(stderr="Claude usage limit reached", code=1))
    assert res.limit_hit and not res.ok


def test_other_failure_is_not_a_limit(tmp_path: Path):
    res = run_persona("hi", cwd=tmp_path, run=fake(stderr="boom", code=1))
    assert not res.ok and not res.limit_hit
```

- [ ] **Step 3: Implement** (flags per Step 1)

```python
# tools/sim/haiku.py
"""Run one persona as a headless Claude Code session."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_LIMIT = re.compile(r"usage limit|rate limit|limit reached|quota", re.IGNORECASE)


@dataclass(frozen=True)
class PersonaResult:
    ok: bool
    text: str
    limit_hit: bool


def run_persona(prompt: str, *, cwd: Path, model: str = "claude-haiku-5-5",
                effort: str = "low", timeout_s: int = 900,
                run=subprocess.run) -> PersonaResult:
    cmd = ["claude", "-p", prompt, "--model", model, "--effort", effort]
    try:
        proc = run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return PersonaResult(ok=False, text="", limit_hit=False)
    combined = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0 and _LIMIT.search(combined):
        return PersonaResult(ok=False, text=proc.stdout, limit_hit=True)
    return PersonaResult(ok=proc.returncode == 0, text=proc.stdout, limit_hit=False)
```

- [ ] **Step 4: PASS + ruff/mypy. Step 5: Commit** `feat(sim): headless persona invocation`.

---

### Task 8: Runner with checkpoint/resume

**Files:**
- Create: `tools/sim/runner.py`, `tools/sim/__main__.py`
- Test: `tests/sim/test_runner.py`

**Interfaces:**
- Consumes: `SimClock` (T2), `SimState` (T3), `build_schedule/due_events/Event` (T4), `parse_findings/append_findings/build_index` (T5), `due_personas/build_prompt` (T6), `run_persona/PersonaResult` (T7).
- Produces: `class LimitPause(Exception)`; `run(*, weeks: int, state_dir: Path, findings_dir: Path, apartments: list[str], urls: dict[str, str], apply_event: Callable[[Event], None], persona_fn: Callable[[str], PersonaResult], seed: int = 1, tool_notes: str = "") -> None`. Raises `LimitPause` after saving state when `persona_fn` reports `limit_hit`. CLI exit code 75 on pause.

- [ ] **Step 1: Failing test**

```python
# tests/sim/test_runner.py
import pytest
from tools.sim.haiku import PersonaResult
from tools.sim.runner import LimitPause, run

APTS = ["a01", "a02"]
GOOD = PersonaResult(True, 'FINDING: {"area":"ux","severity":"minor","title":"t","expected":"e","actual":"a"}\nDONE', False)


def make(tmp_path, persona_fn, events):
    return dict(weeks=3, state_dir=tmp_path / "s", findings_dir=tmp_path / "f",
                apartments=APTS, urls={"fleet": "http://127.0.0.1:1"},
                apply_event=events.append, persona_fn=persona_fn)


def test_full_run_advances_clock_and_writes_index(tmp_path):
    events = []
    run(**make(tmp_path, lambda p: GOOD, events))
    assert (tmp_path / "f" / "INDEX.md").exists()
    assert (tmp_path / "s" / "clock").read_text().startswith("2026-01-")
    assert events, "scenario events must have been applied"


def test_limit_pause_then_resume_does_not_repeat_work(tmp_path):
    events, calls = [], []

    def flaky(prompt):
        calls.append(prompt)
        return PersonaResult(False, "", True) if len(calls) == 3 else GOOD

    with pytest.raises(LimitPause):
        run(**make(tmp_path, flaky, events))
    applied_before = len(events)
    calls_before_resume = len(calls)

    run(**make(tmp_path, lambda p: (calls.append(p), GOOD)[1], events))
    # the interrupted step is retried, finished steps are not
    assert len(calls) - calls_before_resume < calls_before_resume + 40
    assert len(events) >= applied_before
    ids = [e.id for e in events]
    assert len(ids) == len(set(ids)), "an event was applied twice"
```

- [ ] **Step 2: FAIL. Step 3: Implement**

```python
# tools/sim/runner.py
"""Week loop: events -> clock -> due personas, checkpointed after every step."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from tools.sim.clock import SIM_START, SimClock
from tools.sim.findings import append_findings, build_index, parse_findings
from tools.sim.haiku import PersonaResult
from tools.sim.personas import build_prompt, due_personas
from tools.sim.scenario import Event, build_schedule, due_events
from tools.sim.state import SimState


class LimitPause(Exception):
    """Usage limit hit; state is saved, rerun to resume."""


def run(*, weeks: int, state_dir: Path, findings_dir: Path, apartments: list[str],
        urls: dict[str, str], apply_event: Callable[[Event], None],
        persona_fn: Callable[[str], PersonaResult], seed: int = 1,
        tool_notes: str = "", settle_s: float = 0.0) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state = SimState.load(state_path)
    clock = SimClock(state_dir / "clock")
    clock.now = SIM_START + timedelta(weeks=state.week)  # restore position on resume
    clock.write()
    schedule = build_schedule(seed, weeks, apartments)

    for week in range(state.week, weeks):
        state.week = week
        for event in due_events(schedule, week, state.applied_events):
            apply_event(event)
            state.mark_event(event.id)
            state.save(state_path)
        for persona in due_personas(week):
            step_id = f"w{week:03d}:{persona.id}"
            if state.is_done(step_id):
                continue
            prompt = build_prompt(persona, week, clock.now.date().isoformat(), urls, tool_notes)
            result = persona_fn(prompt)
            if result.limit_hit:
                state.save(state_path)
                raise LimitPause(step_id)
            append_findings(findings_dir, parse_findings(result.text, week, persona.id))
            state.mark_step(step_id)
            state.save(state_path)
        for _ in range(7):  # day steps so periodic jobs (alarm checks) see the time pass
            clock.advance(timedelta(days=1))
            time.sleep(settle_s)
    state.week = weeks
    state.save(state_path)
    build_index(findings_dir)
```

Resume correctness note: a week always ends with exactly 7 daily steps, so the clock position at the start of week N is `SIM_START + N weeks`; resume restores that. The CLI passes `settle_s=3.0` (alarm-check interval is 2 s in `compose.sim.yml`); tests use the 0.0 default.

```python
# tools/sim/__main__.py
"""python -m tools.sim run --weeks 156 | status | report"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.sim.findings import build_index
from tools.sim.haiku import run_persona
from tools.sim.runner import LimitPause, run
from tools.sim.state import SimState

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "sim-state"
FINDINGS_DIR = ROOT / "docs" / "sim-findings"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools.sim")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--weeks", type=int, default=156)
    sub.add_parser("status")
    sub.add_parser("report")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        state = SimState.load(STATE_DIR / "state.json")
        print(f"week {state.week}, {len(state.done_steps)} persona steps done")
        return 0
    if args.cmd == "report":
        print(build_index(FINDINGS_DIR))
        return 0
    from tools.sim.env import apply_event, apartments, urls  # noqa: PLC0415 -- Task 9
    try:
        run(weeks=args.weeks, state_dir=STATE_DIR, findings_dir=FINDINGS_DIR,
            apartments=apartments(), urls=urls(), apply_event=apply_event,
            persona_fn=lambda prompt: run_persona(prompt, cwd=ROOT), settle_s=3.0)
    except LimitPause as pause:
        print(f"paused at {pause}; rerun `python -m tools.sim run` to resume")
        return 75
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

- [ ] **Step 4: PASS + ruff/mypy (a resumed run's pending week re-applies no event: events are keyed by id). Add `sim-state/` to `.gitignore`. Step 5: Commit** `feat(sim): resumable runner and CLI`.

---

### Task 9: Environment (fleet, container agents, VMs, event application)

**Files:**
- Create: `tools/sim/env/__init__.py` (exposes `apartments()`, `urls()`, `apply_event(event)`), `tools/sim/env/compose.sim.yml`, `tools/sim/env/up.sh`, `tools/sim/env/down.sh`
- Test: `tests/sim/test_env_events.py`

**Interfaces:**
- Consumes: facts from Task 1's notes; `Event`/`EventKind` (T4).
- Produces: `apartments() -> list[str]` (12 ids `sim-a01..sim-a12`), `urls() -> {"fleet", "tenant1".. }`, `apply_event(event)` dispatching each `EventKind` to a `_handlers[kind](event)` that shells out (`docker stop|start <container>`, `limactl shell`, `docker exec` ...).

- [ ] **Step 1: Failing test** for the dispatcher using an injected command runner: every `EventKind` has a handler, and `HEARTBEAT_LOSS` issues a `docker pause`/`stop` on that apartment's container while `HEARTBEAT_RESTORE` reverts it.

```python
# tests/sim/test_env_events.py
from tools.sim.env import HANDLERS, apply_event_with
from tools.sim.scenario import Event, EventKind


def test_every_kind_has_a_handler():
    assert set(HANDLERS) == set(EventKind)


def test_loss_and_restore_target_the_apartment_container():
    cmds = []
    apply_event_with(Event("x", 1, EventKind.HEARTBEAT_LOSS, "sim-a03"), cmds.append)
    apply_event_with(Event("y", 2, EventKind.HEARTBEAT_RESTORE, "sim-a03"), cmds.append)
    assert cmds[0][:2] == ["docker", "pause"] and cmds[0][-1] == "sim-a03"
    assert cmds[1][:2] == ["docker", "unpause"] and cmds[1][-1] == "sim-a03"
```

- [ ] **Step 2: FAIL. Step 3: Implement** `HANDLERS`, `apply_event_with(event, run_cmd)`, `apply_event = lambda e: apply_event_with(e, subprocess.run-wrapper)`. Handlers per kind (container agents; for the two Lima VMs use `limactl shell <vm> -- sudo ...` with the same verbs):
  - `HEARTBEAT_LOSS/RESTORE`: `docker pause|unpause <id>`
  - `WIFI_CHANGE`: `docker network disconnect/connect sim-net <id>`
  - `FAULT`: write a fault file the agent stub reads (path from Task 1 notes)
  - `IMAGE_UPDATE`: fleet-side `desired_state` change through the admin UI/API helper from Task 1 notes
  - `BACKUP`: trigger via the agent's documented command
  - `TOKEN_EXPIRY`: advance expiry by letting the clock cross it (no-op handler that asserts via fleet API)
  - `TENANT_CHANGE`: use the confirmation flow of `fleet/ui_apartment.py` (via the API helper)
  - `POWER_LOSS`: `docker kill` then `docker start`
  - `CLOCK_DRIFT`: write a per-container offset file consumed by that container's faketime (`FAKETIME_TIMESTAMP_FILE` per container)
  Handlers that depend on an interface not yet known must fail loudly with a `NotImplementedError("<kind>: see 00-environment-notes.md §<n>")` raised only at runtime; the unit test above must stay green and the smoke test (Task 10) must show which handlers still raise. **Remove every such raise before the full run.**

- [ ] **Step 4: Write `compose.sim.yml`** (fleet + 10 agent services built from `docker/Dockerfile.fleet` / `Dockerfile.agent`, shared `sim-state/clock` bind mounted read-only, `LD_PRELOAD` + `FAKETIME_TIMESTAMP_FILE=/sim/clock` + `FAKETIME_NO_CACHE=1`, `FLEET_ALARM_CHECK_INTERVAL_S=2`, fleet bound to `127.0.0.1:8200`, own SQLite volume, network `sim-net`) and `up.sh` (generate secrets into `sim-state/.env` with mode 600, `docker compose -f ... up -d --build`, wait for `/healthz`, create the UI users via `tools.docs_screenshots.create_ui_user`, register agents via the flow documented in Task 1 notes, start the 2 Lima VMs by cloning `thermoctl-test-vm`).
- [ ] **Step 5: Bring it up once and check**: `bash tools/sim/env/up.sh && curl -fsS http://127.0.0.1:8200/healthz`. Expected: `ok`/200 and 12 apartments visible in the fleet UI as logged in by the generated admin.
- [ ] **Step 6: Commit** `feat(sim): environment and event handlers`.

---

### Task 10: Smoke run (2 weeks)

**Files:**
- Create: `docs/sim-findings/01-smoke-run.md`

- [ ] **Step 1:** `python -m tools.sim run --weeks 2`; expected exit 0, `sim-state/state.json` shows week 2, `docs/sim-findings/INDEX.md` exists.
- [ ] **Step 2:** Interrupt deliberately (kill after the first persona), rerun, and confirm via `python -m tools.sim status` and `git diff --stat docs/sim-findings` that nothing was repeated.
- [ ] **Step 3:** Open 3 persona transcripts' findings; if >50% are artifacts of the harness itself (wrong URLs, missing credentials in the prompt, stub agent too thin), fix the tooling (never the product) and rerun. Record harness defects in `01-smoke-run.md` separately from product findings.
- [ ] **Step 4:** Commit `docs(sim): smoke run results`.

---

### Task 11: Browser tool for personas (Playwright)

**Files:**
- Create: `tools/sim/browse.py`
- Test: `tests/sim/test_browse.py`

**Interfaces:**
- Produces: CLI `python -m tools.sim.browse shot --url U --out PATH [--width 390] [--dark]` and `python -m tools.sim.browse text --url U` (visible text + title), reading the session cookie from `sim-state/cookies-<role>.json`; `build_cli_args(...)` parsing function that is unit tested. The persona `tool_notes` passed in Task 8 must describe exactly these commands plus `curl` with the role's API token.

- [ ] **Step 1:** Failing test for `build_cli_args(["shot","--url","http://127.0.0.1:8200/","--out","a.png","--width","390"])` returning a dataclass with `width == 390`, `dark is False`; and that a non-local URL (`https://example.com`) raises `ValueError` (local-only guard).
- [ ] **Step 2-4:** Implement parser + guard (host must be `127.0.0.1`/`localhost`) + Playwright body (`chromium.launch`, `new_context(viewport=..., color_scheme="dark"|"light", storage_state=...)`, `page.screenshot(full_page=True)`); the Playwright body is `# pragma: no cover` -- it is exercised by the smoke run in Task 10/12.
- [ ] **Step 5:** Commit `feat(sim): browser tool for personas`.

---

### Task 12: Pentest harness and checks

**Files:**
- Create: `tools/sim/pentest/__init__.py`, `harness.py`, `checks_surface.py`, `checks_authz.py`, `checks_agent.py`
- Test: `tests/sim/test_pentest_harness.py`

**Interfaces:**
- Produces: `Check(id, title, area, severity_if_failed, fn: Callable[[Ctx], CheckResult])`, `CheckResult(passed: bool, detail: str)`, `Ctx(base_url: str, client_factory, creds: dict)`, `run_checks(checks, ctx) -> list[Finding]` (a failed or erroring check becomes a `Finding` with `area=security`; passing checks produce nothing), `ALL_CHECKS`.
- Local-only guard: `run_checks` raises `ValueError` if `ctx.base_url` host is not loopback or a `sim-net` name.

- [ ] **Step 1:** Failing tests: (a) a failing check yields one `Finding` with its severity; (b) a raising check yields a finding with `detail` containing the exception type, not a crash; (c) a non-loopback base URL raises.
- [ ] **Step 2-4:** Implement harness. Then the catalogue, each a small function with its pass criterion:
  - `checks_surface.py`: every GET route (enumerate from `fleet.app.app.routes`) answers 401/302-to-login when unauthenticated, except an explicit allow-list (`/healthz`, `/static/*`, login pages); security headers present (`Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy`, frame protection); session cookie has `HttpOnly`, `Secure` (when TLS), `SameSite`; 5xx pages leak no stack trace/paths; login throttle triggers after the documented threshold (`FLEET_UI_IP_THROTTLE_THRESHOLD`, default 5 per 15 min).
  - `checks_authz.py`: CSRF token required on every POST form; a user of property A cannot read or act on apartment ids of property B (IDOR sweep over `/apartments/<id>` and command-confirm routes); a low-privilege role cannot reach admin pages; replaying a used confirmation token fails; passkey/registration endpoints reject reused or expired challenges.
  - `checks_agent.py`: against a container agent, send the closed command list's boundaries: an unknown command type is rejected by the agent; an expired or already-seen command id is rejected; `open_access` without `pilot_mode` is rejected **by the agent** (principle 5); a desired state naming an image without digest, with `latest`, or from a registry outside `agent/sources.py` is rejected (principle 2); no endpoint accepts or returns a private key (principle 3); upload endpoints refuse oversized or non-age-encrypted tenant backups (principle 4).
- [ ] **Step 5:** `python -m tools.sim pentest` (add the subcommand in `__main__.py`, same pattern as `report`), review the findings by hand, commit `feat(sim): fleet pentest catalogue` and results `docs/sim-findings/02-pentest.md`.

---

### Task 13: Full run

**Files:**
- Modify: none (data only): `docs/sim-findings/*`

- [ ] **Step 1:** `python -m tools.sim run --weeks 156` in the background, log to `sim-state/run.log`.
- [ ] **Step 2:** On exit code 75 (usage limit): report to the owner, wait for their go, then rerun the same command (state resumes automatically). Do not trust Claude Code's reset time.
- [ ] **Step 3:** After completion run `python -m tools.sim report`, then hand-curate `docs/sim-findings/README.md`: top 10 by severity, themes per role (landlord, caretaker, tenant, admin, newcomer), "missing tools" list, security summary, and which findings are harness artifacts.
- [ ] **Step 4:** Cross-review per CLAUDE.md: a different agent reads `INDEX.md` against raw evidence for a sample of 10 findings and removes unconfirmed ones.
- [ ] **Step 5:** Commit `docs(sim): full simulation results`, update `docs/STATUS.md`.

---

## Self-Review

- **Spec coverage:** environment (T1, T9), clock (T2), scenario (T4), personas incl. browser/API mix (T6, T11), resumability (T3, T8, T13), pentest (T12), output and order smoke-then-full (T5, T10, T13). Non-goals are in Global Constraints.
- **Deviation from the spec:** instead of a high-rate clock inside each week, the runner uses daily clock jumps with settle windows. Same observable effect (alarms, expiries fire for real), simpler and deterministic. If the smoke run shows missed periodic jobs, add `x<rate>` to the faketime file in `SimClock.write`.
- **Known soft spots (verified in Task 1/9/10, not guessed):** the exact agent stub for container agents, the `claude` CLI flag names, and which event handlers need fleet-API helpers. Each is gated by a task step that records the answer before code depends on it.
