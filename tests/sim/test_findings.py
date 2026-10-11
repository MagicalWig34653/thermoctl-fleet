import json
from pathlib import Path

from tools.sim.findings import Area, Severity, append_findings, build_index, parse_findings

LINE = (
    'FINDING: {"area":"ux","severity":"major","title":"Button hidden",'
    '"expected":"visible","actual":"cut off at 390px","evidence":"shot-12.png"}'
)


def test_parse_reads_valid_and_ignores_garbage() -> None:
    text = f'hello\n{LINE}\nFINDING: not json\nFINDING: {{"area":"bogus"}}\n'
    found = parse_findings(text, week=12, persona="tenant1")
    assert len(found) == 1
    f = found[0]
    assert (f.week, f.persona, f.area, f.severity) == (12, "tenant1", Area.UX, Severity.MAJOR)


def test_parse_ignores_non_object_json() -> None:
    assert parse_findings("FINDING: [1, 2]\nFINDING: 5\n", 1, "p") == []


def test_index_groups_by_severity_then_area(tmp_path: Path) -> None:
    f = parse_findings(LINE, 3, "landlord")
    append_findings(tmp_path, f)
    append_findings(tmp_path, f)  # appending twice must not duplicate in the index
    index = build_index(tmp_path).read_text()
    assert index.count("Button hidden") == 1
    assert index.index("## major") < index.index("### ux")
    rows = [json.loads(x) for x in (tmp_path / "raw" / "findings.jsonl").read_text().splitlines()]
    assert len(rows) == 2


def test_index_without_findings_file(tmp_path: Path) -> None:
    index = build_index(tmp_path).read_text()
    assert "no findings yet" in index.lower()
