"""Structured findings: persona output -> JSONL -> INDEX.md."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

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
            raw = json.loads(line[len(_PREFIX) :])
            found.append(
                Finding(
                    week=week,
                    persona=persona,
                    area=Area(raw["area"]),
                    severity=Severity(raw["severity"]),
                    title=raw["title"],
                    expected=raw.get("expected", ""),
                    actual=raw.get("actual", ""),
                    evidence=raw.get("evidence", ""),
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


def build_index(out_dir: Path) -> Path:
    source = out_dir / "raw" / "findings.jsonl"
    rows: list[dict[str, Any]] = []
    if source.exists():
        rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:  # same title in the same area counts once, first week wins
        seen.setdefault((row["area"], row["title"]), row)
    lines = ["# Simulation findings\n"]
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
    path = out_dir / "INDEX.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
