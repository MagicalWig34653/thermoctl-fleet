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


def test_prompt_contains_contract_and_forbids_deliberate_attacks():
    p = PERSONAS[0]
    text = build_prompt(p, 5, "2026-02-09", {"fleet": "http://127.0.0.1:8100"}, "notes")
    assert "FINDING: " in text and "2026-02-09" in text and p.character in text
    assert "http://127.0.0.1:8100" in text
    assert "never deliberately attack" in text.lower()
    assert "area security" in text
    assert "curl" not in text
