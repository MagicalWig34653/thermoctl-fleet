# Implementation plan

Work packages for implementing the scaffold, sized to the order from
[`docs/specification.md`](specification.md), section 11. Each package is
sized so that it is **one task with its own worktree** -- not larger.
Following the working method from `CLAUDE.md`: its own branch, cross-review
with a real test run, commit including the updated `docs/STATUS.md`, check
the box here only after the review has passed.

Security-relevant packages (marked **SR**) are additionally read back in the
main session (principle 7 / the six security principles in `CLAUDE.md`).

The order of the sections below follows steps 1-5 from section 11. Packages
without a dependency note can, once their precondition is met, run in
parallel with all other packages of the same stage.

---

## Step 1 -- Webhook receiver (cloud, no change to thermoctl)

### P1.1 -- Token check per apartment
- **Goal:** check `Authorization: Bearer …` against the apartment's stored
  token from the address (`{apartment}`), before an endpoint begins its
  actual work.
- **Files:** `fleet/app.py` (dependency/middleware for `receive_heartbeat`,
  `receive_event`, `commands_stream`, `receive_command_result`), a new
  module for the check itself.
- **Section:** 4, 18.1.
- **Acceptance:** a call without or with a wrong token returns `401`/`403`,
  with a valid token the call passes through unchanged to the existing
  `NotImplementedError`. A test for both cases for each affected endpoint.
- **Parallel to:** nothing (precondition for P1.2, P2.1, P4.x).
- [x] done

### P1.2 -- Finish `POST /v1/events/{apartment}`
- **Goal:** store the event and evaluate it via `fault_kind_from_key`;
  treat an unknown prefix as "other report", never as an error.
- **Files:** `fleet/app.py::receive_event`, storage layer (see P1.3).
- **Section:** 6, 8, 18.1, 22.1 (key table).
- **Acceptance:** all six fault kinds from section 22.1 plus an unknown
  prefix are covered by a test; the test in particular confirms the special
  case "sensor fault and stuck reading share the same key".
- **Depends on:** P1.1, P1.3.
- [x] done

### P1.3 -- Storage layer (database)
- **Goal:** set up persistence for heartbeats, events, inventory -- the
  specification and `STATUS.md` do not fix a schema, that is created here.
  Database choice and migration tool are open; orient on section 12
  (retention: proposal 90 days for heartbeats, 365 days for faults -- clear
  with the project owner before implementing, "proposal" is not a
  decision).
- **Files:** new module, e.g. `fleet/storage.py`, plus a migrations
  directory.
- **Section:** 12.
- **Acceptance:** an event and a heartbeat can be written and read back;
  the test runs against a real, even if lightweight, database (no mock).
- **Parallel to:** P1.1.
- [x] done

---

## Step 2 -- Heartbeat and absence alarming

### P2.1 -- Finish `POST /v1/heartbeat`
- **Goal:** accept the heartbeat, store it, flag it as "outdated version"
  if `protocol_version` is lower than our own (section 18.2 -- "a field may
  only ever be added").
- **Files:** `fleet/app.py::receive_heartbeat`.
- **Section:** 5, 18.2.
- **Acceptance:** test with equal, lower, and higher `protocol_version`;
  the higher one must not be rejected (forward compatibility).
- **Depends on:** P1.1, P1.3.
- [x] done

### P2.2 -- Absence alarming
- **Goal:** if an apartment's heartbeat fails to arrive, alarm (section 8).
- **Files:** new module for the check (background task/scheduler),
  connection to a notification (channel left open per section 8 -- clarify
  with the project owner before hard-coding a service, principle 1 from
  thermoctl's CLAUDE.md).
- **Section:** 8.
- **Acceptance:** test simulates absence over time (no real waiting),
  checks that exactly one alarm fires, not again on every check run.
- **Depends on:** P2.1.
- [x] done -- `fleet/alarms.py` (check function + `WebhookNotifier`/
  `SmtpNotifier`/`LogNotifier`), migration `0004_alarms`, background task
  in `fleet/app.py`'s lifespan. See `docs/STATUS.md` for channels, env
  variables, and open points.

### P2.3 -- Agent: collect and send heartbeat
- **Goal:** implement `agent/loop.py::collect_heartbeat` (thermoctl REST
  client) and `send_heartbeat` (TLS pinning, catch-up delivery after an
  outage, buffered up to 240 entries).
- **Files:** `agent/loop.py`, a new thermoctl client (`agent/thermoctl_client.py`
  or similar).
- **Section:** 3, 4, 5, 10.
- **Acceptance:** test against a stubbed thermoctl `/api/v1/health`
  endpoint (fixture, no real service); the 240-entry buffer limit is
  demonstrated by a test.
- **Parallel to:** P2.1, P2.2 (the other end of the line).

---

## Step 3 -- UI

### P3.1 -- "The house" view
- **Goal:** overview of all apartments with status.
- **Files:** `fleet/` templates/views (directory not yet created),
  `fleet/app.py`.
- **Section:** 9.
- **Acceptance:** page loads, shows every apartment with heartbeat age and
  open faults; test via FastAPI's HTTP client.
- **Depends on:** P1.3, P2.1.

### P3.2 -- "One apartment" view
- **Goal:** detail view of a single apartment.
- **Files:** as P3.1.
- **Section:** 9.
- **Acceptance:** page shows an apartment's history, open faults, and last
  commands.
- **Depends on:** P3.1 (shared templates/navigation).

### P3.3 -- "Inventory" view
- **Goal:** fourth view for property/apartment/device/assignment.
- **Files:** as P3.1, reads `fleet/app.py`'s inventory endpoints (see
  P4.x).
- **Section:** 20.4.
- **Acceptance:** page shows all four entities; test checks that every
  reference in it is reachable (follow the `tests/test_smoke_test.py`
  pattern from thermoctl).
- **Depends on:** P4.1 (at least a reading inventory endpoint).
- **Parallel to:** P3.1, P3.2 once their templates exist.

---

## Step 4 -- Inventory management (section 20)

The six endpoints in `fleet/app.py` are laid out; none checks or enforces
anything. Split by the three rules from section 20.3, so that no package
touches all six endpoints at once.

### P4.1 -- Read inventory, register device
- **Goal:** implement `read_inventory`, `register_device` (reading and
  plain creation respectively, none of the three rules from 20.3 apply).
- **Files:** `fleet/app.py`, P1.3's storage layer.
- **Section:** 20.1-20.3.
- **Acceptance:** test registers a device and reads it back via
  `read_inventory`.
- **Depends on:** P1.3.

### P4.2 -- Prepare device, confirm registration and assign
- **Goal:** implement `prepare_device`, `confirm_device_registration`,
  including "no release without a confirmed verification code" (20.3) and
  "a device belongs to at most one apartment" on assignment.
- **Files:** `fleet/app.py`.
- **Section:** 15.3, 20.3.
- **Acceptance:** test demonstrates both rules negatively (an assignment
  attempt without a confirmed verification code fails; a double assignment
  fails).
- **Depends on:** P4.1.

### P4.3 -- Replace device, change state
- **Goal:** implement `replace_device`, `change_device_state`, including
  "at most one active device per apartment".
- **Files:** `fleet/app.py`.
- **Section:** 15 (device swap), 20.3.
- **Acceptance:** test demonstrates: a second active device for the same
  apartment is rejected; a state transition in `DeviceLifecycle` outside
  the seven allowed values is already excluded by Pydantic (only the
  transition itself needs checking here, e.g. "decommissioned" not
  reversible, if the specification requires it -- otherwise an open point
  for `STATUS.md`, not an invention of our own).
- **Depends on:** P4.1.
- **Parallel to:** P4.2.

---

## Step 5 -- SSE channel and stage-1 commands **SR**

### P5.1 -- SSE channel `GET /v1/commands`
- **Goal:** implement `fleet/app.py::commands_stream` and
  `agent/loop.py::receive_commands`: SSE delivery, `Last-Event-ID`
  reconnection, 60-second poll fallback on an interrupted connection.
- **Files:** `fleet/app.py`, `agent/loop.py`.
- **Section:** 3, 7.
- **Acceptance:** test holds an SSE connection, interrupts it, confirms
  the 60-second poll as the fallback.
- **Depends on:** P1.1.

### P5.2 -- Command execution in the agent **SR**
- **Goal:** implement `agent/loop.py::execute_command` for the four
  stage-1 commands (`REPORT_NOW`, `FETCH_LOGS`, `BACKUP_NOW`,
  `AGENT_RESTART`) plus id and expiry checking, a local log per command and
  rejection.
- **Files:** `agent/loop.py`, persistence for `executed_ids` across
  restarts (open point from the `AgentState` docstring -- to be resolved
  here).
- **Section:** 7.
- **Acceptance:** test per command type, plus: a duplicate id is rejected,
  an expired command is rejected, an unknown command cannot even be
  constructed for lack of a `CommandType` value (a model test is enough
  here).
- **Depends on:** P5.1.
- **Read back by:** main session (agent security boundary, principle 5).

### P5.3 -- Result reporting and `create_diagnostic_bundle`
- **Goal:** implement `agent/loop.py::report_result` and
  `create_diagnostic_bundle` (stage 1), including masking of
  credentials/tenant data in the collected logs. **SR** because of the
  masking (section 21.5, docstring in `loop.py`).
- **Files:** `agent/loop.py`, `fleet/app.py::receive_command_result`.
- **Section:** 21.5.
- **Acceptance:** test demonstrates that a known secret pattern (example
  token, not real) appears masked in the generated bundle.
- **Depends on:** P5.2.
- **Read back by:** main session (masking is security-relevant).

### P5.4 -- Desired-state reconciliation **SR**
- **Goal:** complete `agent/loop.py::reconcile_desired_state` (pre-check,
  backup, digest against the hard-coded source list, swap, 15-minute
  deadline, rollback).
- **Files:** `agent/loop.py`, a new module for the source list
  (`agent/sources.py` or similar -- **a constant in the agent package**,
  not from the cloud, security principle 2).
- **Section:** 13.
- **Acceptance:** test demonstrates: a missing digest prevents the start;
  a digest from an unlisted source is rejected; absence of health after
  15 minutes triggers a rollback.
- **Depends on:** P5.1 (the desired state arrives over the same channel as
  commands, see section 13).
- **Read back by:** main session (the digest check is the central
  safeguard from security principle 2).

### P5.5 -- Backup and restore **SR**
- **Goal:** implement `agent/loop.py::create_backup` for both kinds of
  backup, including encrypting the operational-data backup before upload
  (security principle 4) plus the counterpart "restore" (section 15.2),
  which so far has no stub at all.
- **Files:** `agent/loop.py`, a new module for encryption.
- **Section:** 15.1, 15.2.
- **Acceptance:** test demonstrates that an operational-data backup is not
  readable without a valid local key (real encryption/decryption, no
  mock); device configuration stays plain text, test demonstrates the
  separate handling.
- **Depends on:** nothing from step 5, can start in parallel with
  P5.1-P5.4.
- **Read back by:** main session (encryption, security principle 4).

### P5.6 -- Watchdog main loop (`watchdog/watch.go`)
- **Goal:** actually implement `AgentStopped`, `StartDigest`,
  `AwaitHealthReport`, `RollBackToProven`. Stays **without** a dependency
  in `go.mod`, production code under 300 lines under the new,
  statements-only counting method (section 18.3; see `docs/STATUS.md` for
  the current figure -- remeasure when P5.6 lands, use `linefile.go` as a
  model for further trimming if needed).
- **Files:** `watchdog/watch.go`, `watchdog/main.go`.
- **Section:** 17, 18.3.
- **Acceptance:** `go test ./...` green, `go vet ./...` clean, the
  contract test (`watchdog/check_contract.sh`) still passing, the line
  count documented.
- **Depends on:** nothing from step 5, separated by language -- can run in
  parallel with any Python package.

### P5.7 -- Wire up the status display (section 23)
- **Goal:** connect `watchdog/leds.go` (`LedSetPattern`) to the states from
  P5.6: which watchdog state triggers which pattern from section 23.2.
- **Files:** `watchdog/watch.go`, `watchdog/leds.go`.
- **Section:** 23.
- **Acceptance:** test demonstrates the correct pattern per state; the
  line count stays under the 300-line production-code limit.
- **Depends on:** P5.6.

---

## Only after operational experience (stage 2, sections 21 and 24)

Per the specification, explicitly not before a heating season of
operational experience and explicit approval by the project owner
(security principle 1 in `CLAUDE.md`). No package here starts without that
approval -- even creating a worktree for it is itself already an exception
that must be announced and justified.

- **`factory_reset`** (section 21.2): backup-before-delete order, key and
  token revocation, re-registration with a new verification code. **SR.**
- **`open_access`** (section 21.4): SSH certificate, back-channel,
  60-minute deadline, logging. The `pilot_mode` rejection is already
  actually implemented and stays that way -- this package only builds what
  is still missing after that. **SR.**
- **`esim_profiles_list`, `esim_profile_load`** (section 24.3): `lpac`
  integration, activation-code handling without a log trace.
- **`esim_profile_activate`** (sections 24.3, 24.4): the fallback clock --
  the file format for this is decided (`esim_previous_profile=`/
  `esim_deadline=` as two further lines in the existing state file, see
  `docs/STATUS.md`); this package calls
  `agent.loop.report_watchdog_state` with these and builds the rest of the
  function. **SR.**
- **`esim_profile_delete`** (section 24.3).
- **A/B system partitions** (section 21.3): explicitly deferred per the
  project owner, no package until a heating season shows that on-site
  visits actually are caused by the operating system.
- **eSIM at scale** (section 24.5): only after the trial run on one card/
  device required by section 24.5.
