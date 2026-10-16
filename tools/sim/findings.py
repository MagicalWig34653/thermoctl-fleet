"""Structured findings: persona output -> JSONL -> INDEX.md."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

_PREFIX = "FINDING:"
_NOISE = " \t-*\u2022`"


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


def _text(raw: dict[str, Any], key: str, default: str | None = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise TypeError(key)
    return value


def parse_findings(text: str, week: int, persona: str) -> list[Finding]:
    found: list[Finding] = []
    for line in text.splitlines():
        stripped = line.strip(_NOISE)
        if not stripped.startswith(_PREFIX):
            continue
        try:
            raw = json.loads(stripped[len(_PREFIX) :].strip(_NOISE))
            found.append(
                Finding(
                    week=week,
                    persona=persona,
                    area=Area(raw["area"]),
                    severity=Severity(raw["severity"]),
                    title=_text(raw, "title"),
                    expected=_text(raw, "expected", ""),
                    actual=_text(raw, "actual", ""),
                    evidence=_text(raw, "evidence", ""),
                )
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return found


def append_findings(out_dir: Path, findings: list[Finding]) -> None:
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with (raw_dir / "findings.jsonl").open("a", encoding="utf-8") as handle:
        for finding in findings:
            handle.write(json.dumps(asdict(finding), ensure_ascii=False) + "\n")


def _load_row(line: str) -> dict[str, Any] | None:
    """One JSONL line as a conforming row, or None (truncated, foreign, wrong types)."""
    try:
        raw = json.loads(line)
        if not isinstance(raw, dict):
            return None
        Area(raw["area"])
        Severity(raw["severity"])
        if not isinstance(raw["week"], int) or not isinstance(raw["persona"], str):
            return None
        for key in ("title", "expected", "actual", "evidence"):
            if not isinstance(raw[key], str):
                return None
    except (ValueError, KeyError, TypeError):
        return None
    return raw


def _md_title(path: Path) -> str:
    """First `# ` heading of a markdown file, else the file stem."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip() or path.stem
    return path.stem


def _link_block(out_dir: Path) -> list[str]:
    """Pointers to the consolidated report, curation notes and pentest results."""
    block: list[str] = []
    if (out_dir / "README.md").is_file():
        block.append("Start with the consolidated report: [README.md](README.md).")
    notes = sorted((out_dir / "curation").glob("*.md")) if (out_dir / "curation").is_dir() else []
    notes = [n for n in notes if not re.search(r" \d+\.md$", n.name)]
    if notes:
        links = ", ".join(f"[{_md_title(n)}](curation/{n.name})" for n in notes)
        block.append(f"Curation notes (clusters, verdicts, evidence): {links}.")
    if (out_dir / "pentest" / "results.md").is_file():
        block.append("Pentest: [pentest/results.md](pentest/results.md).")
    if block:
        block.append("The list below is the raw, unfiltered persona output (claimed severities).")
        return ["\n".join(block) + "\n"]
    return []


def build_index(out_dir: Path) -> Path:
    source = out_dir / "raw" / "findings.jsonl"
    rows: list[dict[str, Any]] = []
    if source.exists():
        for line in source.read_text(encoding="utf-8").splitlines():
            row = _load_row(line)
            if row is not None:
                rows.append(row)
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:  # same title in the same area counts once, first week wins
        title_key = " ".join(row["title"].casefold().split())
        seen.setdefault((row["area"], title_key), row)
    lines = ["# Simulation findings\n"]
    lines.extend(_link_block(out_dir))
    if not seen:
        lines.append("No findings yet.\n")
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
    hand = (
        sorted(
            f
            for f in out_dir.glob("[0-9][0-9]-*.md")
            # not the notes, not Finder's "name 2.md" copies
            if f.name != "00-environment-notes.md" and not re.search(r" \d+\.md$", f.name)
        )
        if out_dir.is_dir()
        else []
    )
    if hand:
        lines.append("## Hand-written findings\n")
        for f in hand:
            lines.append(f"- [{_md_title(f)}]({f.name})")
        lines.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "INDEX.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
