"""Desired state for the four containers of an apartment (section 13).

The cloud **states** which revision is desired -- applying it is done exclusively
by the agent, with local pre-checks and a backup (procedure in section 13). The
four service names are fixed: "The agent knows exactly these four names; anything
else is rejected and reported." Hence named fields here instead of an open
`dict[str, ServiceState]` -- a fifth service name simply cannot be transported with
this model.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, Field


class ServiceState(BaseModel):
    """Image, version, and digest of a single service.

    Per section 13 the agent starts **only** images whose digest matches the
    desired state -- "latest" or a tag without a digest is rejected. `digest` is
    therefore not a "should have" here, but a required field.
    """

    image: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(min_length=1, pattern=r"^sha256:[0-9a-f]{64}$")


class Services(BaseModel):
    """The four known services of a base station, taken literally from section 13."""

    thermoctl: ServiceState
    zigbee2mqtt: ServiceState
    mosquitto: ServiceState
    agent: ServiceState


class UpdateWindow(BaseModel):
    """Time window and weather condition for updates (section 13)."""

    from_: time
    until: time
    not_below_outdoor_temp_c: float


class DesiredState(BaseModel):
    """The complete desired state of an apartment, as the cloud holds it."""

    revision: int = Field(ge=0)
    services: Services
    window: UpdateWindow
