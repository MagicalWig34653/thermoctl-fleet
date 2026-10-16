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


def test_prompt_says_the_login_is_done_and_names_the_role():
    by_id = {p.id: p for p in PERSONAS}
    text = build_prompt(by_id["caretaker"], 1, "2026-01-12", {"fleet": "http://x"}, "notes")
    assert "already logged in" in text and "--role caretaker" in text
    newcomer = build_prompt(by_id["newcomer"], 1, "2026-01-12", {"fleet": "http://x"}, "notes")
    assert "--role landlord" in newcomer  # the newcomer works with a landlord account


def test_env_urls_name_the_ui_landing_page():
    from tools.sim import env

    assert env.urls()["fleet web app (browser pages)"] == "http://127.0.0.1:8200/ui/"


def _tenant_urls():
    return {
        "fleet": "http://127.0.0.1:8200",
        "tenant1": "http://127.0.0.1:8301",
        "tenant2": "http://127.0.0.1:8302",
        "tenant3": "http://127.0.0.1:8303",
    }


def test_env_urls_include_the_three_tenant_uis():
    from tools.sim import env

    urls = env.urls()
    assert [urls[f"tenant{n}"] for n in (1, 2, 3)] == [
        "http://127.0.0.1:8301",
        "http://127.0.0.1:8302",
        "http://127.0.0.1:8303",
    ]


def test_tenant_prompt_names_only_its_own_app_and_its_own_login_role():
    by_id = {p.id: p for p in PERSONAS}
    text = build_prompt(by_id["tenant2"], 2, "2026-01-19", _tenant_urls(), "notes")
    assert "http://127.0.0.1:8302" in text and "--role tenant2" in text
    assert "8301" not in text and "8303" not in text  # no other household
    assert "8200" not in text  # a tenant never sees the cloud


def test_other_personas_do_not_see_tenant_apps():
    by_id = {p.id: p for p in PERSONAS}
    text = build_prompt(by_id["landlord"], 1, "2026-01-12", _tenant_urls(), "notes")
    assert "http://127.0.0.1:8200" in text and "830" not in text


def test_mia_is_told_to_use_phone_width():
    mia = {p.id: p for p in PERSONAS}["tenant3"]
    assert "--width 390" in mia.character
