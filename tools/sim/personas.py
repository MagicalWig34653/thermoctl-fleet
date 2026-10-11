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
    Persona(
        "landlord",
        "landlord",
        "Frau Keller, 58, owns three buildings, not technical, "
        "wants one glance at the whole portfolio.",
        [
            "See which apartments have problems",
            "Understand an alarm without technical terms",
            "Hand a flat to a new tenant",
        ],
        1,
        True,
        25,
    ),
    Persona(
        "caretaker",
        "caretaker",
        "Herr Demir, 44, caretaker, practical, uses a phone on site, no patience for jargon.",
        [
            "Find the apartment with a fault and what to do",
            "Run a maintenance command",
            "Fetch a diagnostic bundle",
        ],
        1,
        True,
        25,
    ),
    Persona(
        "tenant1",
        "tenant",
        "Anna, 29, tech-savvy, uses the tenant UI daily.",
        ["Change a schedule", "Check why it is cold", "Look at history"],
        2,
        True,
        20,
    ),
    Persona(
        "tenant2",
        "tenant",
        "Herr Schulz, 71, large fonts, easily confused, uses a tablet.",
        ["Set the room warmer", "Find out whether the heating is broken"],
        2,
        True,
        20,
    ),
    Persona(
        "tenant3",
        "tenant",
        "Mia, 22, phone only, impatient, moves out in the second year.",
        ["Quick temperature change", "Absence period for a holiday"],
        2,
        True,
        20,
    ),
    Persona(
        "admin",
        "admin",
        "Jonas, fleet administrator, careful, reads logs, cares about security and retention.",
        ["Review users and passkeys", "Check backups and retention", "Plan a rollout"],
        4,
        True,
        30,
    ),
    Persona(
        "newcomer",
        "newcomer",
        "Sven, new landlord employee, first week, reads no documentation unless forced.",
        ["Onboard a new base station", "Find where to confirm a device"],
        4,
        True,
        25,
    ),
]


def due_personas(week: int) -> list[Persona]:
    """Personas whose cadence falls on this week (week 0 = everyone)."""
    return [p for p in PERSONAS if week % p.every_weeks == 0]


def build_prompt(
    persona: Persona,
    week: int,
    sim_date: str,
    urls: dict[str, str],
    tool_notes: str,
) -> str:
    """Render the English role-play prompt for one persona session."""
    goals = "\n".join(f"- {g}" for g in persona.goals)
    endpoints = "\n".join(f"- {name}: {url}" for name, url in urls.items())
    finding = (
        '{"area":"logic|ux|looks|missing_tool|security",'
        '"severity":"blocker|major|minor|nit","title":"short",'
        '"expected":"what you hoped for","actual":"what happened",'
        '"evidence":"url or screenshot name"}'
    )
    return (
        "You are role-playing a real person using a heating-control system. "
        "Stay in character.\n\n"
        f"Who you are: {persona.character}\n"
        f"Role: {persona.role}. Today's simulated date: {sim_date} (week {week}).\n"
        f"What you want to get done this session:\n{goals}\n\n"
        "Systems (local test instances only; do not attack or probe anything, "
        f"just use it as this person would):\n{endpoints}\n\n"
        f"How to work: {tool_notes}\n"
        f"Use at most {persona.max_steps} actions. Do not invent data you cannot see.\n\n"
        "When something is wrong, confusing, ugly, missing, or too slow, "
        "report it as one line:\n"
        f"FINDING: {finding}\n"
        "Report a missing capability you wished you had as area missing_tool. "
        "Finish with one line: DONE\n"
    )
