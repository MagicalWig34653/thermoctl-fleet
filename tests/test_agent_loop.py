"""Tests the section-21 stubs in `agent/loop.py`.

`open_access` is the only one with a real check (the `pilot_mode` rejection) --
accordingly two cases for it, not just one. `create_diagnostic_bundle` used to
be one of these stubs (P5.2/P5.3a) -- it is now genuinely implemented (P5.3b);
its own tests live in `tests/test_agent_diagnostic_bundle.py`, mirroring
`create_backup`'s own split into `tests/test_agent_backup.py` (unit) and
`tests/test_agent_backup_e2e.py` (real fleet app, real crypto).
"""

from __future__ import annotations

import pytest

from agent.loop import (
    esim_profile_activate,
    esim_profile_delete,
    esim_profile_load,
    esim_profiles_list,
    factory_reset,
    open_access,
)


def test_factory_reset_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        factory_reset()


def test_open_access_rejects_without_pilot_mode_locally() -> None:
    """Section 21.4: 'the agent rejects the command -- the check is local,

    not in the UI.' This rejection is really implemented, hence
    `PermissionError` rather than `NotImplementedError`.
    """

    with pytest.raises(PermissionError):
        open_access(pilot_mode=False)


def test_open_access_reports_missing_implementation_when_pilot_mode_set() -> None:
    """With `pilot_mode=True` the command passes the one real check --

    the rest (SSH certificate, back-channel) is still a placeholder.
    """

    with pytest.raises(NotImplementedError):
        open_access(pilot_mode=True)


def test_esim_profiles_list_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        esim_profiles_list()


def test_esim_profile_load_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        esim_profile_load("LPA:1$rsp.example.com$ABCDEF")


def test_esim_profile_activate_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        esim_profile_activate("profile-1")


def test_esim_profile_delete_reports_missing_implementation() -> None:
    with pytest.raises(NotImplementedError):
        esim_profile_delete("profile-1")
