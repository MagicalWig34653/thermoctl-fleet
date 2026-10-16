"""Sim findings 12, 13, 20-24: docs switch, heartbeat caps, nesting, headers,
templated UI 404, poisoned stored heartbeat rows."""

from __future__ import annotations

import copy
import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from fleet import hardening
from fleet.app import app
from fleet.storage import HeartbeatRecord, Storage, create_storage, get_storage, upgrade
from protocol import Heartbeat
from tests.test_fleet import APARTMENT, HEARTBEAT_EXAMPLE


@pytest.fixture
def storage(tmp_path: object) -> Storage:
    url = f"sqlite:///{tmp_path}/hardening.db"
    upgrade(url)
    return create_storage(url)


@pytest.fixture
def client(storage: Storage) -> Iterator[TestClient]:
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def auth(storage: Storage) -> dict[str, str]:
    token = f"agent_{APARTMENT}_{secrets.token_urlsafe(32)}"
    storage.set_apartment_token(APARTMENT, token)
    return {"Authorization": f"Bearer {token}"}


JSON_HEADER = {"Content-Type": "application/json"}

# -- F24 ----------------------------------------------------------------------


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_clock_drift_is_rejected_at_ingest(
    client: TestClient, auth: dict[str, str], literal: str
) -> None:
    payload = copy.deepcopy(HEARTBEAT_EXAMPLE)
    payload["system"]["clock_drift_s"] = "PLACEHOLDER"  # type: ignore[index]
    body = json.dumps(payload).replace('"PLACEHOLDER"', literal)
    assert literal in body
    response = client.post("/v1/heartbeat", content=body, headers={**auth, **JSON_HEADER})
    assert response.status_code == 422
    assert "input" not in response.json()["detail"][0]


def test_poisoned_stored_row_does_not_break_the_read_side(storage: Storage) -> None:
    older = {**HEARTBEAT_EXAMPLE, "sent_at": "2026-09-22T14:00:00Z"}
    newer = {**HEARTBEAT_EXAMPLE, "sent_at": "2026-09-22T14:02:00Z"}
    storage.save_heartbeat(
        APARTMENT, Heartbeat.model_validate(older), datetime(2026, 9, 22, 14, 0, tzinfo=UTC)
    )
    storage.save_heartbeat(
        APARTMENT, Heartbeat.model_validate(newer), datetime(2026, 9, 22, 14, 2, tzinfo=UTC)
    )
    # What an older version stored for NaN: `clock_drift_s: null`.
    poisoned = copy.deepcopy(newer)
    poisoned["system"]["clock_drift_s"] = None  # type: ignore[index]
    with storage.session() as session:
        session.execute(
            update(HeartbeatRecord)
            .where(HeartbeatRecord.sent_at > datetime(2026, 9, 22, 14, 1))
            .values(payload_json=json.dumps(poisoned))
        )
    latest = storage.get_latest_heartbeat(APARTMENT)
    assert latest is not None
    assert latest.heartbeat.sent_at == datetime(2026, 9, 22, 14, 0, tzinfo=UTC)
    assert len(storage.list_heartbeats(APARTMENT)) == 1
    assert storage.get_house_overview() is not None


def test_only_poisoned_rows_means_no_latest_heartbeat(storage: Storage) -> None:
    storage.save_heartbeat(
        APARTMENT, Heartbeat.model_validate(HEARTBEAT_EXAMPLE), datetime.now(UTC)
    )
    with storage.session() as session:
        session.execute(update(HeartbeatRecord).values(payload_json="{}"))
    assert storage.get_latest_heartbeat(APARTMENT) is None


# -- F22 / F23 ----------------------------------------------------------------


def test_overlong_free_text_is_rejected(client: TestClient, auth: dict[str, str]) -> None:
    payload = {**HEARTBEAT_EXAMPLE, "agent": "x" * 5000}
    assert client.post("/v1/heartbeat", json=payload, headers=auth).status_code == 422


def test_oversized_body_is_413_by_content_length(client: TestClient, auth: dict[str, str]) -> None:
    body = json.dumps({**HEARTBEAT_EXAMPLE, "agent": "x" * 200_000})
    response = client.post("/v1/heartbeat", content=body, headers={**auth, **JSON_HEADER})
    assert response.status_code == 413


def test_oversized_streamed_body_is_413_without_content_length(
    client: TestClient, auth: dict[str, str]
) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b" " * 10_000

    response = client.post("/v1/heartbeat", content=chunks(), headers={**auth, **JSON_HEADER})
    assert response.status_code == 413


def test_body_cap_is_configurable(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(hardening.MAX_HEARTBEAT_BODY_ENV, "100")
    assert client.post("/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=auth).status_code == 413
    monkeypatch.setenv(hardening.MAX_HEARTBEAT_BODY_ENV, "0")
    with pytest.raises(ValueError):
        client.post("/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=auth)


def test_normal_heartbeat_still_accepted(client: TestClient, auth: dict[str, str]) -> None:
    assert client.post("/v1/heartbeat", json=HEARTBEAT_EXAMPLE, headers=auth).status_code == 204


def test_batch_endpoint_has_its_own_cap(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(hardening.MAX_HEARTBEAT_BATCH_BODY_ENV, "1000")
    response = client.post("/v1/heartbeats", json=[HEARTBEAT_EXAMPLE] * 5, headers=auth)
    assert response.status_code == 413


def test_deeply_nested_json_is_a_4xx_not_a_500(client: TestClient, auth: dict[str, str]) -> None:
    body = "[" * 5000 + "]" * 5000
    for path in ("/v1/heartbeat", "/v1/heartbeats"):
        response = client.post(path, content=body, headers={**auth, **JSON_HEADER})
        assert 400 <= response.status_code < 500
    assert client.get("/healthz").status_code == 200


# -- F20 / F21 ----------------------------------------------------------------


def test_api_responses_carry_hardening_headers(client: TestClient) -> None:
    for path in ("/healthz", "/v1/commands"):
        headers = client.get(path).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["content-security-policy"] == "default-src 'none'"


def test_413_response_carries_headers_too(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post("/v1/heartbeat", content=b" " * 200_000, headers=auth)
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"


def test_ui_headers_are_kept_and_nosniff_added(client: TestClient) -> None:
    headers = client.get("/ui/login").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["content-security-policy"] == "default-src 'self'; script-src 'self'"


# -- F12 ----------------------------------------------------------------------


def test_api_docs_are_off_by_default(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_api_docs_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(hardening.API_DOCS_ENV, raising=False)
    assert hardening.fastapi_docs_kwargs() == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }
    monkeypatch.setenv(hardening.API_DOCS_ENV, "true")
    assert hardening.fastapi_docs_kwargs() == {}


# -- F13 ----------------------------------------------------------------------


def test_browser_404_under_ui_is_a_templated_page(client: TestClient) -> None:
    response = client.get("/ui/nonexistent", headers={"Accept": "text/html,*/*"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Diese Seite gibt es nicht" in response.text
    assert 'href="/ui/"' in response.text


def test_non_browser_404_stays_json(client: TestClient) -> None:
    response = client.get("/ui/nonexistent")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_404_outside_ui_stays_json_even_for_browsers(client: TestClient) -> None:
    response = client.get("/nope", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
