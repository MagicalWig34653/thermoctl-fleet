"""Protocol version of the fleet contract.

Independent of the version numbers of the two images (`fleet`, `agent`) and of the
controlled `thermoctl` itself. Section 18.2's "a number that increases with every
change to the models" is read **literally** (project owner decision, 2026-09-26,
see docs/specification.md 18.2 and protocol/registration.py): any change to a
model in this package bumps it, including a purely additive, backward-compatible
one -- a wholly new model counts as a change to "the models" too. The agent does
not send it separately in the heartbeat; here it serves exclusively to coordinate
between the two packages of this repository and future compatibility checks.
"""

# 2: adds the P4.2b registration models (RegistrationAccepted, TokenChallenge,
# TokenRequest, TokenIssued) -- see protocol/registration.py and the project
# owner's 2026-09-26 decision recorded there and in docs/specification.md 18.2.
PROTOCOL_VERSION = 2
