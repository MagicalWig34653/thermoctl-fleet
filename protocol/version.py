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
# 4: adds `protocol.commands.LogExcerpt` (P5.3a, sections 6, 7, 21.5) -- the
# `fetch_logs` upload body (already-filtered lines, a dropped-line count,
# source, capture time). A wholly new model counts as "a change to the
# models" too, per the project owner's 2026-09-26 literal reading of this
# rule (see above) -- see protocol/commands.py and docs/STATUS.md's P5.3a
# section.
# 5: adds `protocol.backups` (`BackupKind`, `BackupUploadAccepted`) for
# `POST /v1/backups` (P5.5a, section 15.1/15.2) -- a wholly new module
# counts as a change to "the models" too, per this file's own opening
# paragraph. Originally also numbered 4 (developed in parallel with P5.3a's
# own `LogExcerpt` addition above) -- re-numbered to 5 when the two
# branches were merged, since both cannot occupy the same version number.
# 6: adds `protocol.diagnostics` (`DiagnosticBundleUploadAccepted`) for
# `POST /v1/commands/{id}/bundle` (P5.3b, sections 15.1, 21.5) -- a wholly
# new module, same "counts as a change to the models" reading as every
# prior bump above.
# 7: adds `protocol.registration.RegistrationRequest.age_recipient` and the
# wholly new `protocol.restore` module (`AgeRecipientReport`,
# `PendingRestore`, `RestoreResult`) for P5.5b (section 15.2/15.3's
# "Decided afterward" paragraph, 2026-09-28: the device generates its own
# age key pair and registers only the public recipient). An additive field
# on an existing model counts as "a change to the models" too, per this
# file's own opening paragraph's literal reading -- see
# `protocol/registration.py` and `protocol/restore.py`.
# 8: adds `protocol.desired_state.DesiredStateEvent` and
# `DesiredStateOutcomeReport` (P5.4b, section 13) -- the SSE `desired_state`
# event payload and the `POST /v1/desired-state/result` report body. Both
# wholly new models, same "counts as a change to the models" reading as
# every prior bump above; see `protocol/desired_state.py` and
# `docs/STATUS.md`'s P5.4b section.
# 9: adds `protocol.heartbeat.PerDeviceState` and
# `DeviceState.per_device` (P6.3, section 12's "Decided afterward",
# 2026-10-01: "Battery and signal per device may be transmitted ..."). A
# wholly new model plus an additive field on an existing one -- same
# "counts as a change to the models" reading as every prior bump above; see
# `protocol/heartbeat.py` and `docs/STATUS.md`'s P6.3 section.
PROTOCOL_VERSION = 9
