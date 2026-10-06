"""The rebuilt UI shell and pages (phase 1 of the draft-based redesign):
base template (sidebar, topbar, badges), login + 2FA as one form, Übersicht,
Wohnungen, Aufgaben, the icon set and the navigation-count helper.

Real app, real migrated SQLite database, real session login -- same
approach as `tests/test_ui_overview.py`, whose fixtures and helpers are
reused here.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from fleet import ui_nav, ui_routes, webauthn_auth
from fleet.storage import Storage, create_storage, get_storage, upgrade
from fleet.ui_auth import generate_totp_secret, hash_password
from fleet.ui_nav import NavCounts, build_nav_counts, nav_counts_from_overview
from fleet.ui_overview import build_overview
from protocol.backups import BackupKind
from tests.conftest import store_encrypted_totp_secret
from tests.test_ui_overview import (
    APARTMENT_A,
    APARTMENT_B,
    BASE_TIME,
    USERNAME,
    _desired_state,
    _extract_hidden_field,
    _login,
    _make_heartbeat,
)


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/rebuild-test.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def password() -> str:
    return secrets.token_urlsafe(16)


@pytest.fixture
def totp_secret() -> str:
    return generate_totp_secret()


@pytest.fixture
def user_id(storage: Storage, password: str, totp_secret: str) -> int:
    record = storage.create_ui_user(
        username=USERNAME,
        password_hash=hash_password(password),
        totp_secret="",
        created_at=datetime.now(UTC),
    )
    store_encrypted_totp_secret(storage, record.id, totp_secret)
    return record.id


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    from fleet.app import app

    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_storage, None)


TEMPLATES = ui_routes._TEMPLATES_DIR
STATIC = ui_routes._STATIC_DIR


def _seed_property(storage: Storage, *ids: str) -> int:
    prop = storage.create_property("Lindenstraße 12", "Berlin")
    for index, apartment_id in enumerate(ids):
        storage.create_apartment(
            apartment_id,
            property_id=prop.id,
            label=f"Wohnung {index + 1}",
            floor="EG" if index == 0 else "1. OG",
            orientation=None,
            state="occupied",
            heating_circuits=1,
            pilot_mode=False,
        )
    return prop.id


def _stopped_rollout(storage: Storage, apartment: str) -> str:
    storage.create_desired_state_revision(
        apartment, _desired_state(), ui_username="tester", reason="initial", now=BASE_TIME
    )
    rollout = storage.create_rollout(
        service="thermoctl",
        version="1.1",
        digest="sha256:" + "b" * 64,
        apartment_ids=[apartment],
        stagger_hours=48.0,
        timeout_hours=2.0,
        ui_username="tester",
        reason="test",
        now=BASE_TIME,
    )
    storage.start_rollout_apartment(rollout.id, apartment, revision=1, now=BASE_TIME)
    storage.mark_rollout_apartment_failed(
        rollout.id, apartment, reason="agent rejected", now=BASE_TIME
    )
    return rollout.id


# -- icon set ---------------------------------------------------------------------


def _render(template: str, **context: object) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    return env.from_string(template).render(**context)


def test_icon_macro_renders_the_draft_path_and_falls_back_to_the_grid_glyph() -> None:
    bell = _render('{% import "_icons.html" as icons %}{{ icons.icon("bell") }}')
    assert '<svg viewBox="0 0 24 24" aria-hidden="true">' in bell
    assert 'd="M6 9a6 6 0 0 1 12 0c0 7 3 7 3 9H3c0-2 3-2 3-9m7 12h-2"' in bell

    unknown = _render('{% import "_icons.html" as icons %}{{ icons.icon("no-such-icon") }}')
    grid = _render('{% import "_icons.html" as icons %}{{ icons.icon("grid") }}')
    assert unknown == grid


def test_every_icon_used_by_the_templates_exists_in_the_icon_set() -> None:
    icons = _render(
        '{% import "_icons.html" as icons %}'
        "{% for name in names %}{{ icons.icon(name) }}{% endfor %}",
        names=["brand", "user", "lock", "eye", "phone", "key", "close", "logout", "back"],
    )
    # Nine different glyphs, none of them the fallback grid.
    assert icons.count("<svg") == 9
    assert icons.count("M3 3h7v7H3z") == 0


# -- navigation badges helper -----------------------------------------------------


def test_nav_counts_follow_the_real_inbox_and_active_rollouts(storage: Storage) -> None:
    _seed_property(storage, APARTMENT_A, APARTMENT_B)
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, BASE_TIME), BASE_TIME)
    now = BASE_TIME + timedelta(minutes=1)

    quiet = build_nav_counts(storage, now)
    # APARTMENT_B never reported: one task. No rollout yet.
    assert quiet == NavCounts(tasks=1, updates=0, properties=1, stand=quiet.stand)
    assert quiet.stand.startswith("heute, ")

    _stopped_rollout(storage, APARTMENT_A)
    busy = build_nav_counts(storage, now)
    assert busy.tasks == 2  # + the rollout waiting for a decision
    assert busy.updates == 1


def test_nav_counts_from_overview_does_not_query_again(storage: Storage) -> None:
    _seed_property(storage, APARTMENT_A)
    overview = build_overview(storage, BASE_TIME)

    counts = nav_counts_from_overview(overview, BASE_TIME)

    assert counts.tasks == len(overview.inbox)
    assert counts.updates == overview.active_rollouts
    assert counts.properties == overview.property_count


def test_nav_context_is_lazy_and_cached_per_request(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = ui_nav.build_nav_counts

    def counting(storage_arg: Storage, now: datetime) -> NavCounts:
        calls.append(1)
        return real(storage_arg, now)

    monkeypatch.setattr(ui_nav, "build_nav_counts", counting)
    monkeypatch.setattr(ui_nav, "_storage_for", lambda request: storage)

    class _State:
        pass

    class _Request:
        state = _State()

    request = _Request()
    context = ui_nav.nav_context(request)  # type: ignore[arg-type]
    assert calls == []  # nothing computed until a template calls it
    first = context["nav_counts"]()
    second = context["nav_counts"]()
    assert first is second
    assert calls == [1]


def test_nav_context_returns_none_when_the_badge_query_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(storage_arg: Storage, now: datetime) -> NavCounts:
        raise RuntimeError("database gone")

    monkeypatch.setattr(ui_nav, "build_nav_counts", broken)
    monkeypatch.setattr(ui_nav, "_storage_for", lambda request: object())

    class _State:
        pass

    class _Request:
        state = _State()

    context = ui_nav.nav_context(_Request())  # type: ignore[arg-type]
    assert context["nav_counts"]() is None


def test_storage_for_honours_dependency_overrides(client: TestClient, storage: Storage) -> None:
    from fleet.app import app

    class _Request:
        pass

    request = _Request()
    request.app = app  # type: ignore[attr-defined]
    assert ui_nav._storage_for(request) is storage  # type: ignore[arg-type]


# -- base shell ---------------------------------------------------------------------


def _login_and_get(client: TestClient, password: str, totp_secret: str, path: str = "/ui/") -> str:
    _login(client, password, totp_secret)
    response = client.get(path)
    assert response.status_code == 200
    return response.text


def test_shell_has_sidebar_nav_badges_account_and_logout(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _seed_property(storage, APARTMENT_A, APARTMENT_B)
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, BASE_TIME), BASE_TIME)
    _stopped_rollout(storage, APARTMENT_A)

    html = _login_and_get(client, password, totp_secret)

    for href, label in (
        ("/ui/", "Übersicht"),
        ("/ui/apartments", "Wohnungen"),
        ("/ui/tasks", "Aufgaben"),
        ("/ui/inventory", "Einrichtung"),
        ("/ui/rollouts", "Updates"),
    ):
        assert re.search(
            rf'<a class="nav-link[^"]*" href="{re.escape(href)}"[^>]*>.*?{label}', html, re.S
        )
    # Badges come from real data: 2 tasks (never reported + stopped rollout), 1 update.
    tasks_link = re.search(r'href="/ui/tasks".*?</a>', html, re.S)
    assert tasks_link is not None and '<span class="count">2' in tasks_link.group(0)
    updates_link = re.search(r'href="/ui/rollouts".*?</a>', html, re.S)
    assert updates_link is not None and '<span class="count">1' in updates_link.group(0)
    # Active page is marked.
    assert re.search(r'class="nav-link active" href="/ui/" aria-current="page"', html)
    # Account block links to Konto, logout is a CSRF-protected POST form.
    assert 'class="account" href="/ui/account/webauthn"' in html
    assert re.search(
        r'<form class="logout-form" method="post" action="/ui/logout">\s*'
        r'<input type="hidden" name="csrf_token" value="[^"]+">',
        html,
    )
    assert "Fleet-Dienst erreichbar" in html
    assert "Letzter Stand · heute, " in html
    assert "Meine Liegenschaften" in html
    assert "Portfolio · 1 Gebäude" in html
    # Breadcrumb and bell.
    assert "<b>Übersicht</b>" in html
    assert 'aria-label="Offene Aufgaben (2)"' in html
    # Brand mark + favicon (the draft's icon, not the old app icon).
    assert 'class="brandmark"' in html
    assert '<link rel="icon" type="image/svg+xml" href="/ui/static/favicon.svg">' in html
    # Home-screen icon: the Icon Composer app icon, rendered for iOS.
    assert '<link rel="apple-touch-icon" href="/ui/static/apple-touch-icon.png">' in html


def test_mobile_menu_works_as_a_plain_link_without_javascript(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    html = _login_and_get(client, password, totp_secret)

    # The hamburger is a link to the sidebar (:target), the shade a link away from it.
    assert re.search(r'<a class="icon-button mobile-menu" href="#sidebar"', html)
    assert '<aside class="sidebar" id="sidebar"' in html
    assert re.search(r'<a class="menu-shade" href="#main"', html)
    assert '<main id="main"' in html


@pytest.mark.parametrize(
    "path",
    [
        "/ui/", "/ui/apartments", "/ui/tasks", "/ui/inventory",
        "/ui/rollouts", "/ui/rollouts/new", "/ui/account/webauthn",
    ],
)
def test_pages_satisfy_the_csp_no_inline_style_script_or_handler(
    client: TestClient, password: str, totp_secret: str, user_id: int, path: str
) -> None:
    html = _login_and_get(client, password, totp_secret, path)

    assert " style=" not in html
    assert "<style" not in html
    assert not re.search(r"\son[a-z]+=", html)
    for script in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in script
    # No demo remnants from the draft.
    for forbidden in ("INTERAKTIVER ENTWURF", "Demodaten", "Login-Demo", "Democode"):
        assert forbidden not in html


@pytest.mark.parametrize(
    "path", ["/ui/inventory", "/ui/rollouts", "/ui/rollouts/new", "/ui/account/webauthn"]
)
def test_rebuilt_pages_drop_the_legacy_wrapper(
    client: TestClient, password: str, totp_secret: str, user_id: int, path: str
) -> None:
    html = _login_and_get(client, password, totp_secret, path)
    assert '<main id="main" class="main">' in html
    assert "/ui/static/legacy.css" in html

    rebuilt = client.get("/ui/").text  # same session: a TOTP code may not be replayed
    assert '<main id="main" class="main">' in rebuilt


def test_static_assets_of_the_rebuild_are_served_same_origin(client: TestClient) -> None:
    for name, content_type in (
        ("fleet-ui.css", "text/css"),
        ("legacy.css", "text/css"),
        ("auth.css", "text/css"),
        ("shell.js", "javascript"),
        ("rollout-confirm.js", "javascript"),
        ("auth.js", "javascript"),
        ("favicon.svg", "image/svg+xml"),
        ("apple-touch-icon.png", "image/png"),
    ):
        response = client.get(f"/ui/static/{name}")
        assert response.status_code == 200, name
        assert content_type in response.headers["content-type"], name
    # The old Archivo font files are gone.
    assert client.get("/ui/static/fonts/Archivo-Regular.ttf").status_code == 404


def test_design_system_stylesheet_is_light_only_with_the_draft_tokens() -> None:
    css = (STATIC / "fleet-ui.css").read_text(encoding="utf-8")

    for token in (
        "--bg:#f5f6f3",
        "--paper:#fff",
        "--ink:#243d37",
        "--green:#31755d",
        "--red:#b74c40",
    ):
        assert token in css
    assert "prefers-color-scheme" not in css
    assert "Archivo" not in css
    assert "@font-face" not in css
    assert "INTERAKTIVER" not in css
    # Section markers later phases rely on.
    for section in (
        "TOKENS & BASE",
        "APP SHELL",
        "PAGE: Uebersicht",
        "PAGE: Wohnungen",
        "PAGE: Aufgaben",
    ):
        assert section in css


# -- Übersicht ---------------------------------------------------------------------


def test_overview_renders_metrics_cards_buildings_feed_and_rollout(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    now = datetime.now(UTC)
    _seed_property(storage, APARTMENT_A, APARTMENT_B)
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, now), now)
    storage.create_backup_record(
        APARTMENT_A,
        BackupKind.OPERATIONAL_DATA,
        size_bytes=5,
        content_hash="c" * 64,
        storage_path="x",
        now=now - timedelta(minutes=5),
    )
    _stopped_rollout(storage, APARTMENT_A)

    html = _login_and_get(client, password, totp_secret)

    assert '<div class="eyebrow">Mein Portfolio</div>' in html
    assert "<h1>Alles im Blick.</h1>" in html
    assert "1 Liegenschaft, 2 Wohnungen. Ein zentraler Überblick." in html
    assert re.search(r'class="button primary" href="/ui/inventory"', html)
    # Four metric cards with real numbers.
    assert re.search(r'Wohnungen .*?<div class="metric-number">2</div>', html, re.S)
    assert re.search(r'Erreichbar .*?<div class="metric-number">1 <small>/ 2</small>', html, re.S)
    assert "50 % verbunden" in html
    assert re.search(
        r'Letzte Sicherungen .*?<div class="metric-number">1 <small>/ 2</small>', html, re.S
    )
    # "Hier braucht es Sie": real inbox items, link to all tasks.
    assert '<a class="text-button" href="/ui/tasks">Alle Aufgaben' in html
    assert "Noch nie gemeldet" in html
    assert "Rollout wartet auf Entscheidung" in html
    # Building card: heading link, one window per apartment, colour by state.
    assert "Lindenstraße 12" in html
    assert len(re.findall(r'class="window[ "]', html)) == 2
    assert 'class="window error" href="/ui/apartments/' + APARTMENT_B in html
    assert 'class="window" href="/ui/apartments/' + APARTMENT_A in html
    assert "! 1 Auffälligkeit" in html
    # Feed shows the real backup; rollout card the real rollout.
    assert "Sicherung erfolgreich" in html
    assert "Lindenstraße 12 · Wohnung 1" in html
    assert "Softwareverteilung" in html
    assert 'max="1" value="0"' in html
    assert re.search(r'href="/ui/rollouts/[0-9a-f-]+">Rollout ansehen', html)
    # No section-6 categories anywhere.
    for forbidden in ("°C", "Solltemperatur", "Mieter:"):
        assert forbidden not in html


def test_overview_hides_the_rollout_card_without_an_active_rollout(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _seed_property(storage, APARTMENT_A)
    storage.save_heartbeat(
        APARTMENT_A, _make_heartbeat(APARTMENT_A, datetime.now(UTC)), datetime.now(UTC)
    )

    html = _login_and_get(client, password, totp_secret)

    assert "Softwareverteilung" not in html
    assert "Noch keine Ereignisse." in html
    assert 'class="bottom-grid single"' in html


def test_overview_of_an_empty_fleet_is_calm_and_offers_setup(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    html = _login_and_get(client, password, totp_secret)

    assert "Noch keine Wohnung eingerichtet." in html
    assert "Nichts zu tun – es ist noch keine Wohnung eingerichtet." in html
    assert re.search(r'href="/ui/inventory">Wohnung einrichten', html)


def test_overview_list_view_switch_works_without_javascript(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _seed_property(storage, APARTMENT_A, APARTMENT_B)
    _login(client, password, totp_secret)

    buildings = client.get("/ui/").text
    listing = client.get("/ui/?ansicht=liste").text

    assert 'class="buildings"' in buildings
    assert "<table>" not in buildings
    assert 'class="buildings"' not in listing
    assert "<table>" in listing
    assert APARTMENT_A in listing and APARTMENT_B in listing
    assert re.search(r'<a class="active" href="/ui/\?ansicht=liste" aria-current="true">', listing)
    assert re.search(r'<a class="active" href="/ui/" aria-current="true">', buildings)
    # Any unknown value falls back to the building view.
    assert 'class="buildings"' in client.get("/ui/?ansicht=quatsch").text


# -- Wohnungen -----------------------------------------------------------------------


def test_apartments_page_has_the_draft_toolbar_table_and_keeps_filters(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    prop_id = _seed_property(storage, APARTMENT_A, APARTMENT_B)
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, BASE_TIME), BASE_TIME)
    _login(client, password, totp_secret)

    page = client.get("/ui/apartments").text

    assert re.search(r'<form class="toolbar" method="get" action="/ui/apartments"', page)
    for field in ('name="q"', 'name="property"', 'name="state"'):
        assert field in page
    assert '<button class="button primary" type="submit">Filtern</button>' in page
    assert "<small>EG</small>" in page  # floor under the apartment name
    assert "2 von 2 Wohnungen" in page

    narrowed = client.get(f"/ui/apartments?property={prop_id}&state=never_reported&q=wohnung").text
    assert APARTMENT_B in narrowed
    assert f'href="/ui/apartments/{APARTMENT_A}"' not in narrowed
    assert "1 von 2 Wohnungen" in narrowed
    assert "Filter zurücksetzen" in narrowed

    empty = client.get("/ui/apartments?q=gibt-es-nicht").text
    assert "Keine Wohnung entspricht dieser Suche." in empty


def test_apartments_page_of_an_empty_fleet_says_so(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    html = _login_and_get(client, password, totp_secret, "/ui/apartments")

    assert "Noch keine Wohnungen eingerichtet." in html
    assert "0 von 0 Wohnungen" in html


# -- Aufgaben ------------------------------------------------------------------------


def test_tasks_page_requires_login(client: TestClient) -> None:
    response = client.get("/ui/tasks", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_tasks_page_lists_every_inbox_item_with_the_side_panel(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _seed_property(storage, APARTMENT_A, APARTMENT_B)
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, BASE_TIME), BASE_TIME)
    rollout_id = _stopped_rollout(storage, APARTMENT_A)

    html = _login_and_get(client, password, totp_secret, "/ui/tasks")

    assert "<h1>Das steht an.</h1>" in html
    assert "2 offene Aufgaben in Ihrem Portfolio." in html
    assert html.count('class="issue"') == 2
    assert f'href="/ui/rollouts/{rollout_id}"' in html
    assert "Priorität hat Erreichbarkeit." in html
    assert "Verbindung &amp; Störungen" in html
    assert re.search(r'badge error">1 offen', html)  # the never-reported apartment
    assert "Wartet" in html  # rollout waits for a decision
    assert re.search(r'class="nav-link active" href="/ui/tasks" aria-current="page"', html)


def test_tasks_page_without_tasks_is_calm(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _seed_property(storage, APARTMENT_A)
    now = datetime.now(UTC)
    storage.save_heartbeat(APARTMENT_A, _make_heartbeat(APARTMENT_A, now), now)

    html = _login_and_get(client, password, totp_secret, "/ui/tasks")

    assert "Nichts zu tun – die Wohnung ist in Ordnung." in html
    assert "Nichts fällig." in html
    assert "Keine laufende" in html
    assert "nichts offen" in html


# -- apartment detail + confirmation pages (phase 2a) ------------------------------------


def test_place_text_prefers_property_and_label_then_label_then_id() -> None:
    from fleet.ui_house import place_text

    assert place_text("Lindenstraße 12", "Wohnung 03", "lin-w03") == "Lindenstraße 12 · Wohnung 03"
    assert place_text(None, "Wohnung 03", "lin-w03") == "Wohnung 03"
    assert place_text("Lindenstraße 12", None, "lin-w03") == "lin-w03"
    assert place_text(None, None, "lin-w03") == "lin-w03"


def test_apartment_page_is_rebuilt_with_a_human_heading_and_the_id_as_secondary_text(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int
) -> None:
    _seed_property(storage, APARTMENT_A, APARTMENT_B)

    html = _login_and_get(client, password, totp_secret, f"/ui/apartments/{APARTMENT_B}")

    assert '<main id="main" class="main">' in html  # no legacy wrapper any more
    assert "<h1>Lindenstraße 12 · Wohnung 2</h1>" in html
    assert f'<code class="id-text">{APARTMENT_B}</code>' in html
    assert '<div class="eyebrow">1. OG</div>' in html
    assert 'aria-label="Wohnungsbereiche"' in html
    for tab in ("ueberblick", "wartung", "technik"):
        assert f'href="?ansicht={tab}"' in html


def test_unknown_apartment_page_is_a_404_with_the_rebuilt_shell(
    client: TestClient, password: str, totp_secret: str, user_id: int
) -> None:
    _login_and_get(client, password, totp_secret, "/ui/")
    response = client.get("/ui/apartments/gibt-es-nicht")

    assert response.status_code == 404
    assert "Diese Wohnung ist nicht bekannt." in response.text
    assert "Alle Wohnungen" in response.text


@pytest.mark.parametrize("tab", ["ueberblick", "wartung", "technik"])
def test_apartment_tabs_satisfy_the_csp_and_keep_every_form_action(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int, tab: str
) -> None:
    _seed_property(storage, APARTMENT_A)
    path = f"/ui/apartments/{APARTMENT_A}?ansicht={tab}"
    html = _login_and_get(client, password, totp_secret, path)

    assert " style=" not in html
    assert "<style" not in html
    assert not re.search(r"\son[a-z]+=", html)
    for script in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in script
    for forbidden in ("INTERAKTIVER ENTWURF", "Demodaten", "Demo-"):
        assert forbidden not in html
    # Every command stays a plain link to its confirmation page.
    if tab == "wartung":
        commands = ("report_now", "fetch_logs", "backup_now", "agent_restart", "diagnostic_bundle")
        for command in commands:
            assert f'href="/ui/apartments/{APARTMENT_A}/commands/{command}/confirm"' in html
        assert 'href="/ui/apartments/' + APARTMENT_A + '/tenant-change/confirm"' in html


@pytest.mark.parametrize(
    "suffix",
    [
        "/commands/report_now/confirm",
        "/commands/fetch_logs/confirm",
        "/tenant-change/confirm",
        "/desired-state/edit",
    ],
)
def test_confirmation_pages_are_rebuilt_cards_with_csrf_and_a_cancel_link(
    client: TestClient, storage: Storage, password: str, totp_secret: str, user_id: int, suffix: str
) -> None:
    _seed_property(storage, APARTMENT_A)
    html = _login_and_get(client, password, totp_secret, f"/ui/apartments/{APARTMENT_A}{suffix}")

    assert '<main id="main" class="main">' in html
    assert 'class="confirm-card' in html
    assert re.search(r'<form method="post" action="/ui/apartments/[^"]+"', html)
    assert 'name="csrf_token"' in html
    assert ">Abbrechen</a>" in html
    assert " style=" not in html
    assert not re.search(r"\son[a-z]+=", html)


# -- login + 2FA as one form ------------------------------------------------------------


def test_login_is_one_form_with_both_steps_and_the_unchanged_field_names(
    client: TestClient,
) -> None:
    html = client.get("/ui/login").text

    assert html.count("<form") == 1
    assert '<form method="post" action="/ui/login" id="login-form"' in html
    for field in ("pre_csrf", "webauthn_assertion", "webauthn_challenge_id"):
        assert f'name="{field}"' in html
    for field, kind in (("username", "text"), ("password", "password"), ("totp_code", "text")):
        assert re.search(rf'name="{field}" type="{kind}"', html), field
    # Step 1 and step 2 live in the same form; step switching is client-side only.
    assert 'data-pane="1"' in html and 'data-pane="2"' in html
    assert 'id="auth-flow" data-step="1"' in html
    assert 'autocomplete="one-time-code"' in html
    assert 'inputmode="numeric"' in html
    assert 'autocomplete="current-password"' in html
    # Required password outside a passwordless network; one plain submit without JS.
    assert re.search(r'name="password"[^>]*required', html)
    assert re.search(r'<button class="button primary has-arrow no-js-only" type="submit"', html)
    # Six decorative boxes mirror one real input.
    assert html.count('class="code-box"') == 6
    # Draft copy, no demo hints.
    for text in (
        "Willkommen zurück.",
        "Noch ein sicherer Schritt.",
        "Weiter zur Bestätigung",
        "Bestätigen &amp; anmelden",
        "Temperaturen und Mieterdaten bleiben in der Wohnung.",
        "Ihre Liegenschaften.",
    ):
        assert text in html
    for forbidden in ("Democode", "Nur zum Ausprobieren", "INTERAKTIVER ENTWURF", "Demo"):
        assert forbidden not in html
    assert " style=" not in html
    assert "<style" not in html
    assert '<script src="/ui/static/auth.js"></script>' in html


def test_login_script_does_not_talk_to_the_server_before_step_two() -> None:
    js = (STATIC / "auth.js").read_text(encoding="utf-8")

    # No oracle: the step switch never issues a request and never reads the password's value.
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "password.value"):
        assert forbidden not in js
    assert "checkValidity" in js  # only "is it filled in"
    assert "requestSubmit" in js  # auto-submit of the one real form


def test_passwordless_login_keeps_the_hint_and_makes_the_password_optional(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_routes, "is_passwordless_network", lambda ip: True)
    monkeypatch.setattr(webauthn_auth, "is_configured", lambda: True)

    html = client.get("/ui/login").text

    assert 'id="passwordless-hint"' in html
    assert "Sie sind in einem bekannten Netz" in html
    assert not re.search(r'name="password"[^>]*required', html)
    assert "nur ohne Passkey nötig" in html
    # Passkey button keeps the id webauthn.js addresses, and the script is loaded.
    assert 'id="webauthn-login-button"' in html
    assert 'id="webauthn-error"' in html
    assert '<script src="/ui/static/webauthn.js"></script>' in html
    assert "Mit Passkey anmelden" in html and "Mit Passkey bestätigen" in html


def test_login_without_passkey_support_has_no_passkey_button(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webauthn_auth, "is_configured", lambda: False)

    html = client.get("/ui/login").text

    assert "webauthn-login-button" not in html
    assert "webauthn.js" not in html


def test_failed_login_renders_the_generic_error_on_step_one_and_new_csrf(
    client: TestClient, storage: Storage, user_id: int
) -> None:
    page = client.get("/ui/login")
    pre_csrf = _extract_hidden_field(page.text, "pre_csrf")

    response = client.post(
        "/ui/login",
        data={
            "username": "landlord",
            "password": secrets.token_urlsafe(8),
            "totp_code": "000000",
            "pre_csrf": pre_csrf,
        },
    )

    assert response.status_code == 401
    assert re.search(
        r'<p class="error" role="alert" data-step-only="1">Anmeldung fehlgeschlagen\.',
        response.text,
    )
    assert _extract_hidden_field(response.text, "pre_csrf") != pre_csrf
    assert 'id="auth-flow" data-step="1"' in response.text
    # The server never reveals which factor failed.
    assert "Passwort falsch" not in response.text
    assert "Code falsch" not in response.text


def test_login_page_has_no_sidebar_or_navigation_chrome(client: TestClient) -> None:
    html = client.get("/ui/login").text

    assert 'class="sidebar"' not in html
    assert "nav-link" not in html
    assert "Meine Liegenschaften" not in html
