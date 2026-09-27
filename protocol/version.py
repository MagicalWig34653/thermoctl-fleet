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
# 3: adds `protocol.commands.Command.protocol_version` (P5.1, section 18.2:
# "The agent rejects commands of a newer version it does not know ...
# reports that as a result, and keeps running") -- a `Command` must carry the
# protocol version it was created under so the agent has something to reject
# against; see protocol/commands.py and docs/STATUS.md's P5.1 section.
# 4: adds `protocol.backups` (`BackupKind`, `BackupUploadAccepted`) for
# `POST /v1/backups` (P5.5a, section 15.1/15.2) -- a wholly new module
# counts as a change to "the models" too, per this file's own opening
# paragraph.
PROTOCOL_VERSION = 4
