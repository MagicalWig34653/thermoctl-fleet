"""Tests the manual `DeviceLifecycle` transition table (P4.3,
docs/specification.md section 20.1/20.2 -- a derived reading, see
`fleet/device_lifecycle.py`'s own module docstring for the reasoning
behind each of the five allowed pairs).

Exhaustive: every one of the 7 x 7 = 49 possible `(current, target)` pairs
is checked against the module's own explicit expectation, not just the five
allowed ones and a token handful of refusals.
"""

from __future__ import annotations

import itertools

import pytest

from fleet.device_lifecycle import (
    ALLOWED_MANUAL_DEVICE_TRANSITIONS,
    REMOVE_DEVICE_TARGET_STATES,
    allowed_manual_target_states,
    validate_manual_device_transition,
)
from protocol.inventory import DeviceLifecycle

_ALL_STATE_VALUES = [state.value for state in DeviceLifecycle]

_EXPECTED_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("faulty", "in_storage"),
        ("in_storage", "decommissioned"),
        ("registered", "decommissioned"),
        ("prepared", "decommissioned"),
        ("faulty", "decommissioned"),
    }
)


def test_the_five_allowed_pairs_match_the_derived_reading_exactly() -> None:
    """Pins the module's own table against the work package's derived
    reading verbatim -- if this ever drifts, every other test in this file
    would still pass against the (now wrong) table, so the table itself
    must be pinned too."""

    assert ALLOWED_MANUAL_DEVICE_TRANSITIONS == _EXPECTED_ALLOWED
    assert len(ALLOWED_MANUAL_DEVICE_TRANSITIONS) == 5


@pytest.mark.parametrize(
    "current,target", list(itertools.product(_ALL_STATE_VALUES, _ALL_STATE_VALUES))
)
def test_exhaustive_7x7_transition_table(current: str, target: str) -> None:
    """Every one of the 49 possible pairs, checked against
    `_EXPECTED_ALLOWED` -- an allowed pair returns `None` (no refusal
    message); anything else returns a non-empty message."""

    result = validate_manual_device_transition(current, target)
    if (current, target) in _EXPECTED_ALLOWED:
        assert result is None, f"{current} -> {target} should be allowed, got {result!r}"
    else:
        assert result is not None, f"{current} -> {target} should be refused"
        assert result.strip() != ""


def test_decommissioned_is_terminal_as_a_source_for_every_target() -> None:
    for target in _ALL_STATE_VALUES:
        message = validate_manual_device_transition("decommissioned", target)
        assert message is not None
        assert "Endzustand" in message


def test_in_service_is_never_a_manual_source() -> None:
    """"in_service -> *" is not in the table at all -- only
    `Storage.remove_device` ("Gerät ausbauen/tauschen") may end an
    in_service device's state (see the module's own docstring)."""

    for target in _ALL_STATE_VALUES:
        assert ("in_service", target) not in ALLOWED_MANUAL_DEVICE_TRANSITIONS
        assert validate_manual_device_transition("in_service", target) is not None


def test_no_target_is_ever_prepared_reported_or_in_service() -> None:
    """Transitions *into* prepared/reported/in_service are never manual --
    both P4.2/P4.2b's job (initial commissioning) or
    `Storage.remove_device`'s own job (in_service, via the device-swap flow,
    never this table)."""

    for _, target in ALLOWED_MANUAL_DEVICE_TRANSITIONS:
        assert target not in {"prepared", "reported", "in_service"}


def test_in_storage_to_prepared_is_not_manual_here() -> None:
    """Belongs to P4.2's own "reset" confirmation, not this module."""

    assert ("in_storage", "prepared") not in ALLOWED_MANUAL_DEVICE_TRANSITIONS
    message = validate_manual_device_transition("in_storage", "prepared")
    assert message is not None


def test_unknown_state_values_are_refused_with_a_clear_message() -> None:
    message = validate_manual_device_transition("not-a-real-state", "faulty")
    assert message is not None
    assert "Unbekannter" in message

    message = validate_manual_device_transition("faulty", "not-a-real-state")
    assert message is not None
    assert "Unbekannter" in message


def test_allowed_manual_target_states_matches_the_table() -> None:
    for current in _ALL_STATE_VALUES:
        expected = [
            target
            for target in _ALL_STATE_VALUES
            if (current, target) in ALLOWED_MANUAL_DEVICE_TRANSITIONS
        ]
        assert allowed_manual_target_states(current) == expected


def test_allowed_manual_target_states_empty_for_decommissioned() -> None:
    assert allowed_manual_target_states("decommissioned") == []


def test_allowed_manual_target_states_empty_for_in_service() -> None:
    assert allowed_manual_target_states("in_service") == []


def test_remove_device_target_states_are_faulty_and_in_storage_only() -> None:
    assert set(REMOVE_DEVICE_TARGET_STATES) == {"faulty", "in_storage"}
