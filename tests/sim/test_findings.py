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


def test_parse_requires_string_fields() -> None:
    bad = 'FINDING: {"area":"ux","severity":"major","title":["x"]}'
    bad2 = 'FINDING: {"area":"ux","severity":"major","title":"t","expected":5}'
    assert parse_findings(f"{bad}\n{bad2}\n", 1, "p") == []


def test_parse_tolerates_prefix_noise() -> None:
    body = LINE[len("FINDING: ") :]
    variants = [
        "   " + LINE,
        "FINDING:" + body,
        "- " + LINE,
        "* " + LINE,
        "`" + LINE + "`",
        "- `" + LINE + "`",
    ]
    for v in variants:
        found = parse_findings(v, 1, "p")
        assert len(found) == 1, v
        assert found[0].title == "Button hidden"


def test_index_skips_truncated_and_nonconforming_lines(tmp_path: Path) -> None:
    append_findings(tmp_path, parse_findings(LINE, 3, "landlord"))
    path = tmp_path / "raw" / "findings.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"area": "ux", "severity": "major"}\n')
        handle.write("[1, 2]\n")
        handle.write('{"week": 4, "persona": "x", "area": "ux", "sev')
    index = build_index(tmp_path).read_text()
    assert index.count("Button hidden") == 1


def test_index_dedupes_near_duplicate_titles(tmp_path: Path) -> None:
    a = parse_findings(LINE, 3, "landlord")
    b = parse_findings(LINE.replace("Button hidden", "  button   HIDDEN "), 5, "tenant")
    append_findings(tmp_path, a + b)
    index = build_index(tmp_path).read_text()
    assert index.count("utton") == 1
    assert "week 3" in index
