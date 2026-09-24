"""Registration and rights (section 4) as well as initial setup/device swap (15.3).

Covers three steps that both sides must understand jointly:

1. `AgentRegistrationFile` -- the content of `agent-registration.json`, which an
   image-writing tool places on the boot partition (15.3, step 1).
2. `RegistrationRequest` -- what a freshly started device uses to register with
   the fleet service (15.3, step 2): registration code and its own **public**
   key. The private key never leaves the base station (section 14 applies here
   analogously to registration too, not only to WireGuard).
3. `RegistrationConfirmation` -- the verification code that appears on both
   sides and only releases the configuration after confirmation in the fleet UI
   (15.3, step 3).

The issued token itself (`agent_<apartment>_<random>`, section 4) is a secret and
therefore deliberately **not** a Pydantic model with an example value here -- an
example with a real-looking shape would itself already violate "no secrets in the
repo, not even as a fallback value" (thermoctl-CLAUDE.md, principle 2, adopted
here).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AgentRegistrationFile(BaseModel):
    """Content of `agent-registration.json` on the boot partition (15.3.1)."""

    fleet_address: str = Field(min_length=1)
    certificate_fingerprint: str = Field(min_length=1)
    registration_code: str = Field(min_length=1)


class RegistrationRequest(BaseModel):
    """First contact of a device with the fleet service (15.3.2)."""

    registration_code: str = Field(min_length=1)
    public_key: str = Field(min_length=1)


class RegistrationConfirmation(BaseModel):
    """Verification code that device and fleet UI display independently (15.3.3).

    Only once a human confirms the same verification code in the fleet UI does the
    cloud release the configuration -- `apartment` is therefore only set after
    confirmation, not yet at the request stage.
    """

    verification_code: str = Field(min_length=1)
    apartment: str | None = None
