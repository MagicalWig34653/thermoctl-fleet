"""Week loop: events -> personas -> clock, checkpointed after every step."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path

from tools.sim.clock import SIM_START, SimClock, clock_path
from tools.sim.findings import append_findings, build_index, parse_findings
from tools.sim.haiku import PersonaResult
from tools.sim.personas import PERSONAS, build_prompt, due_personas
from tools.sim.scenario import Event, build_schedule, due_events
from tools.sim.state import SimState

# Exactly the tools a persona session has (see haiku.DEFAULT_ALLOWED_TOOLS).
TOOL_NOTES = (
    "You can only use two commands, both run from a scratch working directory "
    "that contains no project files. "
    "`python -m tools.sim.http GET|POST <url> [--json '<json>'] [--form key=value ...] "
    "[--cookie-jar <file>]` talks to the local systems listed above: --json sends a JSON "
    "body, --form sends form fields (repeat the option), and --cookie-jar stores and "
    "replays cookies; the cookie jar must be a plain file name inside the working "
    "directory (for example `jar.json`), not a path elsewhere. "
    "`python -m tools.sim.browse shot --url <url> --out <name.png> [--width 390] [--dark] "
    "[--role <role>]` takes a full-page screenshot into the working directory and "
    "`python -m tools.sim.browse text --url <url> [--role <role>]` prints the title and "
    "visible text of a page, as a person in a browser would see it."
)

_FAILURE_TEXT_MAX = 500
_STEP_ID = re.compile(r"w\d+:[A-Za-z0-9_-]+")


class LimitPause(Exception):
    """Usage limit hit; state is saved, rerun to resume."""


def _record_skipped_event(state_dir: Path, event: Event, message: str) -> None:
    row = {
        "event": event.id,
        "kind": str(event.kind),
        "apartment": event.apartment,
        "week": event.week,
        "message": message[:_FAILURE_TEXT_MAX],
    }
    with (state_dir / "skipped-events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def skipped_event_count(state_dir: Path) -> int:
    path = state_dir / "skipped-events.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _check_resume_parameters(
    state: SimState, personas: list[str], seed: int, all_ids: list[str]
) -> None:
    """Refuse a resume whose persona filter or seed differs from the one that started the run."""
    # Older state files recorded neither: they were runs with all personas and seed 1.
    recorded_personas = state.personas if state.personas is not None else all_ids
    recorded_seed = state.seed if state.seed is not None else 1
    if state.has_progress():
        if sorted(recorded_personas) != sorted(personas):
            raise ValueError(
                f"state was started with personas {','.join(recorded_personas)} but this run "
                f"asks for {','.join(personas)}; use the same --personas or a fresh state dir"
            )
        if recorded_seed != seed:
            raise ValueError(
                f"state was started with seed {recorded_seed} but this run asks for seed "
                f"{seed}; use the same --seed or a fresh state dir"
            )
    state.personas = list(personas)
    state.seed = seed


def _record_failure(state_dir: Path, step_id: str, week: int, text: str) -> None:
    row = {"step": step_id, "week": week, "text": text[:_FAILURE_TEXT_MAX]}
    with (state_dir / "failures.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _failed_steps(state_dir: Path) -> list[str]:
    path = state_dir / "failures.jsonl"
    if not path.exists():
        return []
    steps: dict[str, None] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            step = json.loads(line)["step"]
        except (ValueError, KeyError, TypeError):
            continue  # a torn last line must not block the run
        if isinstance(step, str) and _STEP_ID.fullmatch(step):
            steps[step] = None  # anything else is a malformed row: skipped
    return list(steps)


def outstanding_failures(state_dir: Path) -> list[str]:
    """Failed persona steps that are not done yet (what the next run will retry)."""
    state = SimState.load(state_dir / "state.json")
    return [step for step in _failed_steps(state_dir) if not state.is_done(step)]


def run(
    *,
    weeks: int,
    state_dir: Path,
    findings_dir: Path,
    apartments: list[str],
    urls: dict[str, str],
    apply_event: Callable[[Event], None],
    persona_fn: Callable[[str], PersonaResult],
    seed: int = 1,
    tool_notes: str = "",
    settle_s: float = 0.0,
    personas: Sequence[str] | None = None,
    refresh_logins: Callable[[], None] | None = None,
) -> None:
    """Run (or resume) the simulation up to `weeks`.

    A week always ends with exactly seven daily clock steps, so the clock at the
    start of week N is SIM_START + N weeks; resume restores that. Events are
    keyed by id and persona steps by `wNNN:<persona>`, so nothing repeats.

    `personas` restricts the run to those ids (default all); it and `seed` are stored
    in the state and a resume with different values raises ValueError. An event whose
    handler raises NotImplementedError is logged to `skipped-events.jsonl` and counted
    as applied; any other exception aborts the run (state saved). `refresh_logins`,
    if given, is called once at the start of every simulated week.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state = SimState.load(state_path)
    all_ids = [p.id for p in PERSONAS]
    selected = list(personas) if personas is not None else all_ids
    unknown = sorted(set(selected) - set(all_ids))
    if unknown:
        raise ValueError(f"unknown personas: {', '.join(unknown)}")
    _check_resume_parameters(state, selected, seed, all_ids)
    state.save(state_path)
    clock = SimClock(clock_path(state_dir))
    clock.now = SIM_START + timedelta(weeks=state.week)
    clock.write()
    schedule = build_schedule(seed, weeks, apartments)

    # Failed steps of earlier runs belong to weeks that already ended; retry
    # them first, with the prompt dated to their own week.
    by_id = {p.id: p for p in PERSONAS}
    for step_id in _failed_steps(state_dir):
        persona_id = step_id.split(":", 1)[1]
        week_of = int(step_id.split(":", 1)[0][1:])
        if state.is_done(step_id) or persona_id not in by_id or persona_id not in selected:
            continue
        retry_date = (SIM_START + timedelta(weeks=week_of)).date().isoformat()
        prompt = build_prompt(by_id[persona_id], week_of, retry_date, urls, tool_notes)
        result = persona_fn(prompt)
        if result.limit_hit:
            raise LimitPause(step_id)
        if not result.ok:
            _record_failure(state_dir, step_id, week_of, result.text)
            continue
        append_findings(findings_dir, parse_findings(result.text, week_of, persona_id))
        state.mark_step(step_id)
        state.save(state_path)

    for week in range(state.week, weeks):
        state.week = week
        if refresh_logins is not None:
            refresh_logins()
        for event in due_events(schedule, week, state.applied_events):
            try:
                apply_event(event)
            except NotImplementedError as exc:
                _record_skipped_event(state_dir, event, str(exc))
            except BaseException:
                state.save(state_path)
                raise
            state.mark_event(event.id)
            state.save(state_path)
        for persona in due_personas(week):
            if persona.id not in selected:
                continue
            step_id = f"w{week:03d}:{persona.id}"
            if state.is_done(step_id):
                continue
            prompt = build_prompt(persona, week, clock.now.date().isoformat(), urls, tool_notes)
            result = persona_fn(prompt)
            if result.limit_hit:
                state.save(state_path)
                raise LimitPause(step_id)
            if not result.ok:
                _record_failure(state_dir, step_id, week, result.text)
                continue  # not marked done: retried on the next run
            append_findings(findings_dir, parse_findings(result.text, week, persona.id))
            state.mark_step(step_id)
            state.save(state_path)
        for _ in range(7):  # day steps so periodic jobs (alarm checks) see time pass
            clock.advance(timedelta(days=1))
            time.sleep(settle_s)
        state.week = week + 1
        state.save(state_path)
    state.week = weeks
    state.save(state_path)
    build_index(findings_dir)
