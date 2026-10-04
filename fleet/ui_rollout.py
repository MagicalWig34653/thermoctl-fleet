"""View models for the rollout UI (P5.4c scope item 3: "a list of
rollouts and a detail page with per-apartment state, times, last
outcome").

Mirrors `fleet/ui_apartment.py`'s own separation: this module derives
plain, template-ready dataclasses from `fleet.storage` rows and does all
German-language rendering; `fleet/ui_routes.py` only wires the
authenticated request to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fleet.storage import RolloutApartmentRecord, Storage
from fleet.ui_format import format_local_datetime, format_technical_reason

ROLLOUT_SERVICE_LABELS: dict[str, str] = {
    "thermoctl": "thermoctl",
    "zigbee2mqtt": "Zigbee2MQTT",
    "mosquitto": "Mosquitto",
    "agent": "Agent",
}

_ROLLOUT_STATE_LABELS: dict[str, str] = {
    "running": "läuft",
    "stopped": "gestoppt",
    "completed": "abgeschlossen",
    "cancelled": "abgebrochen",
}

_APARTMENT_STATUS_LABELS: dict[str, str] = {
    "queued": "wartet",
    "in_progress": "läuft",
    "converged": "erfolgreich",
    "failed": "fehlgeschlagen",
    "timed_out": "Zeitüberschreitung",
    "skipped": "übersprungen",
}


def _format_timestamp(moment: datetime | None) -> str | None:
    """German local date/time (`fleet.ui_format.format_local_datetime`),
    not a raw UTC string -- see that module's own docstring."""

    if moment is None:
        return None
    return format_local_datetime(moment)


def _format_agent_reason(raw: str | None) -> str | None:
    """`stopped_reason`/`last_outcome_reason` are agent-echoed, not
    landlord-authored (unlike `RolloutDetail.reason`, the "Grund" a human
    typed on the start/resume/cancel form, left untouched) -- see
    `fleet.ui_format.format_technical_reason`'s own docstring."""

    if raw is None:
        return None
    return format_technical_reason(raw)


@dataclass(frozen=True)
class RolloutListEntry:
    rollout_id: str
    service_label: str
    version: str
    state: str
    state_label: str
    stopped_reason: str | None
    created_text: str
    created_by: str
    total_apartments: int
    converged_apartments: int


def build_rollout_list(storage: Storage) -> list[RolloutListEntry]:
    entries = []
    for rollout in storage.list_rollouts():
        apartments = storage.rollout_apartments(rollout.id)
        entries.append(
            RolloutListEntry(
                rollout_id=rollout.id,
                service_label=ROLLOUT_SERVICE_LABELS.get(rollout.service, rollout.service),
                version=rollout.version,
                state=rollout.state,
                state_label=_ROLLOUT_STATE_LABELS.get(rollout.state, rollout.state),
                stopped_reason=_format_agent_reason(rollout.stopped_reason),
                created_text=_format_timestamp(rollout.created_at) or "",
                created_by=rollout.created_by,
                total_apartments=len(apartments),
                converged_apartments=sum(1 for a in apartments if a.status == "converged"),
            )
        )
    return entries


@dataclass(frozen=True)
class RolloutApartmentDisplay:
    apartment_id: str
    position: int
    is_pilot: bool
    status: str
    status_label: str
    revision: int | None
    started_text: str | None
    converged_text: str | None
    last_outcome_reason: str | None


def _apartment_display(record: RolloutApartmentRecord) -> RolloutApartmentDisplay:
    return RolloutApartmentDisplay(
        apartment_id=record.apartment_id,
        position=record.position,
        is_pilot=record.is_pilot,
        status=record.status,
        status_label=_APARTMENT_STATUS_LABELS.get(record.status, record.status),
        revision=record.revision,
        started_text=_format_timestamp(record.started_at),
        converged_text=_format_timestamp(record.converged_at),
        last_outcome_reason=_format_agent_reason(record.last_outcome_reason),
    )


@dataclass(frozen=True)
class RolloutDetail:
    rollout_id: str
    service: str
    service_label: str
    version: str
    digest: str
    state: str
    state_label: str
    stopped_reason: str | None
    stagger_hours: float
    timeout_hours: float
    pilot_converged_text: str | None
    created_text: str
    created_by: str
    reason: str
    apartments: list[RolloutApartmentDisplay]
    can_resume: bool
    can_cancel: bool


def build_rollout_detail(storage: Storage, rollout_id: str) -> RolloutDetail | None:
    rollout = storage.get_rollout(rollout_id)
    if rollout is None:
        return None
    apartments = [_apartment_display(a) for a in storage.rollout_apartments(rollout_id)]
    return RolloutDetail(
        rollout_id=rollout.id,
        service=rollout.service,
        service_label=ROLLOUT_SERVICE_LABELS.get(rollout.service, rollout.service),
        version=rollout.version,
        digest=rollout.digest,
        state=rollout.state,
        state_label=_ROLLOUT_STATE_LABELS.get(rollout.state, rollout.state),
        stopped_reason=_format_agent_reason(rollout.stopped_reason),
        stagger_hours=rollout.stagger_hours,
        timeout_hours=rollout.timeout_hours,
        pilot_converged_text=_format_timestamp(rollout.pilot_converged_at),
        created_text=_format_timestamp(rollout.created_at) or "",
        created_by=rollout.created_by,
        reason=rollout.reason,
        apartments=apartments,
        can_resume=rollout.state == "stopped",
        can_cancel=rollout.state in ("running", "stopped"),
    )


__all__ = [
    "ROLLOUT_SERVICE_LABELS",
    "RolloutApartmentDisplay",
    "RolloutDetail",
    "RolloutListEntry",
    "build_rollout_detail",
    "build_rollout_list",
]
