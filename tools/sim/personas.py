"""Persona definitions (Haiku role-play) and the prompt each round gets."""

from __future__ import annotations

import re
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
        "Anna, 29, tech-savvy, uses the tenant UI daily. Likes to see what the system "
        "does and why.",
        [
            "Change the weekly schedule of a room (tab Zeitplan: day/night times)",
            "Check why a room is cold (room cards on Zuhause, tab Verlauf)",
            "Look at the temperature history and heating time of the last days",
        ],
        2,
        True,
        20,
    ),
    Persona(
        "tenant2",
        "tenant",
        "Herr Schulz, 71, large fonts, easily confused, uses a tablet. Reads every word, "
        "is afraid of breaking something, does not know technical terms. Look at pages "
        "with `shot --width 1024` and judge whether the text is big and clear enough.",
        [
            "Make the living room warmer for a while (Zuhause)",
            "Find out whether the heating is broken and say so to the landlord "
            "(Problem melden, or a note that you could not find it)",
        ],
        2,
        True,
        20,
    ),
    Persona(
        "tenant3",
        "tenant",
        "Mia, 22, phone only (always look with `shot --width 390`), impatient, "
        "moves out in the second year.",
        [
            "Quick temperature change in one room (Zuhause, buttons - and +)",
            "Enter an absence period for a holiday on Zuhause, and end it early",
        ],
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


# Browser logins exist per fleet role; the newcomer is a new landlord employee.
LOGIN_ROLE = {"newcomer": "landlord"}


def is_tenant(persona: Persona) -> bool:
    return persona.role == "tenant"


def login_role(persona: Persona) -> str:
    """Browse `--role` of the persona: each tenant has an own login (tenant1..3)."""
    if is_tenant(persona):
        return persona.id
    return LOGIN_ROLE.get(persona.role, persona.role)


def endpoint_lines(persona: Persona, urls: dict[str, str]) -> str:
    """A tenant sees only its own heating app (never the cloud nor another household)."""
    if is_tenant(persona):
        own = urls.get(persona.id)
        label = (
            "your heating app (tenant UI, in German; tabs Zuhause/Zeitplan/Heizzeit/Verlauf/Mehr)"
        )
        shown = {label: own} if own else {}
    else:
        shown = {name: url for name, url in urls.items() if not re.fullmatch(r"tenant\d+", name)}
    return "\n".join(f"- {name}: {url}" for name, url in shown.items())


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
    endpoints = endpoint_lines(persona, urls)
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
        "Systems (local test instances only; use them as this person would. "
        "Never deliberately attack, probe or exploit anything; if normal use "
        "shows something unsafe (another person's data visible, secrets on "
        "screen, something you can do that you should not be able to), report "
        f"it as area security without going further):\n{endpoints}\n\n"
        f"Your browser session is already logged in as {persona.role}: pass "
        f"`--role {login_role(persona)}` to the browse commands "
        "and do not try to log in yourself.\n"
        f"How to work: {tool_notes}\n"
        f"Use at most {persona.max_steps} actions. Do not invent data you cannot see.\n\n"
        "When something is wrong, confusing, ugly, missing, or too slow, "
        "report it as one line:\n"
        f"FINDING: {finding}\n"
        "Report a missing capability you wished you had as area missing_tool. "
        "Finish with one line: DONE\n"
    )
