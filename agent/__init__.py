"""Agent: the device side (docs/specification.md, section 2).

The only program on the base station that talks to the cloud. Reads thermoctl
exclusively via its existing REST interface with its own, read-only token
(`zone.read`, `device.read`, `audit.read`, eventually `health.read` -- section 10)
and decides **locally** which commands it even executes (section 2: the agent is
the security boundary, not the cloud).
"""
