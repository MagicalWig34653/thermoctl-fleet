"""P5.4b: protocol-level tests for the desired-state delivery models
(`protocol/desired_state.py`) and the CommandType closed-list guard
CLAUDE.md security principle 1 requires ("the command list is closed").
"""

from __future__ import annotations

import pydantic
import pytest

from protocol.commands import CommandType
from protocol.desired_state import (
    DesiredState,
    DesiredStateEvent,
    DesiredStateOutcomeReport,
    Services,
    ServiceState,
    UpdateWindow,
)
from protocol.version import PROTOCOL_VERSION

_VALID_DIGEST = "sha256:" + "a" * 64


def _desired_state() -> DesiredState:
    return DesiredState(
        revision=1,
        services=Services(
            thermoctl=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
            zigbee2mqtt=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
            mosquitto=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
            agent=ServiceState(image="x", version="1", digest=_VALID_DIGEST),
        ),
        window=UpdateWindow(from_="09:00", until="16:00", not_below_outdoor_temp_c=-2.0),
    )


# -- CLAUDE.md security principle 1: the command list is closed ---------------


def test_command_type_is_exactly_the_stage_1_set() -> None:
    """A `desired_state` SSE event is explicitly **not** a `CommandType`
    value (P5.4b's own work order) -- this pins the exact, closed set so a
    future change that accidentally adds one (here or anywhere else) is
    caught immediately, not just by a `len()` check."""

    assert {member.value for member in CommandType} == {
        "report_now",
        "fetch_logs",
        "backup_now",
        "agent_restart",
        "diagnostic_bundle",
    }


def test_protocol_version_is_8() -> None:
    """Bumped from 7 (main, P5.5b) for the two wholly new models this
    package adds (`DesiredStateEvent`, `DesiredStateOutcomeReport`) --
    `protocol.version`'s own literal "a wholly new model counts as a
    change to the models too" reading."""

    assert PROTOCOL_VERSION == 8


# -- DesiredStateEvent ----------------------------------------------------------


def test_desired_state_event_round_trips() -> None:
    event = DesiredStateEvent(desired_state=_desired_state(), pilot_mode=True)
    round_tripped = DesiredStateEvent.model_validate_json(event.model_dump_json())
    assert round_tripped == event


def test_desired_state_event_requires_pilot_mode() -> None:
    with pytest.raises(pydantic.ValidationError):
        DesiredStateEvent.model_validate({"desired_state": _desired_state().model_dump()})


def test_desired_state_event_rejects_a_malformed_digest_in_a_service() -> None:
    """The digest pattern is enforced at the model level for every
    service, including through the `DesiredStateEvent` wrapper -- not just
    when a `DesiredState` is constructed directly."""

    payload = _desired_state().model_dump(mode="json")
    payload["services"]["thermoctl"]["digest"] = "sha256:not-hex"
    with pytest.raises(pydantic.ValidationError):
        DesiredStateEvent.model_validate({"desired_state": payload, "pilot_mode": False})


def test_desired_state_event_rejects_a_digest_without_the_sha256_prefix() -> None:
    payload = _desired_state().model_dump(mode="json")
    payload["services"]["thermoctl"]["digest"] = "a" * 64
    with pytest.raises(pydantic.ValidationError):
        DesiredStateEvent.model_validate({"desired_state": payload, "pilot_mode": False})


def test_desired_state_event_rejects_trailing_characters_after_the_digest() -> None:
    """`fullmatch` semantics, not a prefix check -- a value with extra
    trailing bytes after the 64 hex characters must be refused."""

    payload = _desired_state().model_dump(mode="json")
    payload["services"]["thermoctl"]["digest"] = _VALID_DIGEST + "ff"
    with pytest.raises(pydantic.ValidationError):
        DesiredStateEvent.model_validate({"desired_state": payload, "pilot_mode": False})


# -- DesiredStateOutcomeReport ---------------------------------------------------


def test_desired_state_outcome_report_round_trips_with_service() -> None:
    report = DesiredStateOutcomeReport(
        revision=3, successful=True, reason="ok", service="thermoctl"
    )
    round_tripped = DesiredStateOutcomeReport.model_validate_json(report.model_dump_json())
    assert round_tripped == report


def test_desired_state_outcome_report_service_defaults_to_none() -> None:
    report = DesiredStateOutcomeReport(revision=1, successful=False, reason="pilot_mode not set")
    assert report.service is None


def test_desired_state_outcome_report_rejects_a_negative_revision() -> None:
    with pytest.raises(pydantic.ValidationError):
        DesiredStateOutcomeReport(revision=-1, successful=True, reason="ok")


def test_desired_state_outcome_report_carries_no_apartment_field() -> None:
    """Scoping to an apartment happens exclusively via the authenticated
    token (`fleet.app.receive_desired_state_result`), never a
    caller-supplied field this model could carry -- structurally, not just
    "not currently used"."""

    assert "apartment" not in DesiredStateOutcomeReport.model_fields
