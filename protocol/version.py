"""Protocol version of the fleet contract.

Independent of the version numbers of the two images (`fleet`, `agent`) and of the
controlled `thermoctl` itself. Bumped as soon as a field name, a required field, or
the meaning of an existing field changes -- not for purely additive, backward-compatible
extensions. The agent does not send it separately in the heartbeat; here it serves
exclusively to coordinate between the two packages of this repository and future
compatibility checks.
"""

PROTOCOL_VERSION = 1
