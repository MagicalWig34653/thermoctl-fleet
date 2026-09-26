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

### P2.1b -- Catch-up batch endpoint `POST /v1/heartbeats`
- **Goal:** accept a JSON list of buffered heartbeats an agent sends in one
  batch after an outage (section 5, "the agent sends the buffered
  heartbeats (at most the last 240, i.e. eight hours) on next contact, in
  one batch") -- additive per section 18.2, `POST /v1/heartbeat` (P2.1)
  stays exactly as it is. Decided by the project owner (2026-09-24): a new
  endpoint, not a widened body on the singular one; P2.3 (the agent side
  that would produce such a batch) is deferred (see below).
- **Files:** `fleet/app.py::receive_heartbeats_batch` (new), `fleet/storage.py::
  Storage.save_heartbeats_batch` (new), `protocol/heartbeat.py`
  (`MAX_CATCH_UP_HEARTBEATS = 240`, a module constant, not a model field),
  `fleet/migrations/versions/0003_heartbeats_unique_sent_at.py` (new,
  added during cross-review, see below).
- **Section:** 5, 18.2.
- **Acceptance:** at least 1, at most 240 entries accepted, 241 rejected
  with 422, an empty list rejected with 422; every entry must carry the
  authenticated apartment or the whole batch is a 403 and nothing is
  stored; a resent batch and a batch overlapping a heartbeat already
  received live via `POST /v1/heartbeat` both do not produce duplicate
  rows, **including under concurrent/overlapping requests for the same
  apartment** (cross-review reproduced a duplicate-row race in a first,
  Python-level-only idempotency check; fixed with a unique database index,
  migration `0003`, and a dialect-native insert-or-ignore write -- see
  `docs/STATUS.md`); `Storage.get_latest_heartbeat`'s ordering is
  deterministic even when many rows from one batch share the same
  `received_at` (tie broken by `sent_at` then `id`).
- **Depends on:** P2.1.
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
- **Deferred (project owner, 2026-09-24):** until thermoctl provides
  `/api/v1/health` and `health.read` (section 10); no response format is
  invented in this repository.

---

## Step 3 -- UI

### P3.0 -- Login for the fleet UI **SR**
- **Goal:** the landlord can log in to the fleet UI before any of the three
  views (section 9) exist to protect. The specification itself is silent on
  how this login works -- decided by the project owner, 2026-09-24: own user
  accounts in the fleet database, password hashed with Argon2
  (`argon2-cffi`), TOTP as a mandatory second factor (`pyotp`), server-side
  sessions via a cookie; the first account is created via a CLI command,
  never via the web; no external identity provider; passkeys/WebAuthn noted
  as a possible later extension, not built here.
- **Files:** `fleet/ui_auth.py` (auth/session/CSRF logic, the
  `require_ui_user` dependency later UI packages depend on),
  `fleet/ui_routes.py` (`/ui/login`, `/ui/logout`, the protected `/ui/`
  placeholder, the `/ui`-scoped security-header middleware),
  `fleet/admin.py` (`python -m fleet.admin create-user/reset-totp/unlock/
  delete-user`), `fleet/storage.py` (`UiUserRecord`/`UiSessionRecord` plus
  their `Storage` methods), migration `0005_ui_accounts`,
  `fleet/templates/ui/{base,login,index}.html`.
- **Section:** 9 (this login sits in front of all three views); no
  specification section defines it directly, see the decision above.
- **Acceptance:** see `docs/STATUS.md`'s P3.0 section and
  `tests/test_ui_auth.py`/`tests/test_admin.py`/`tests/test_packaging.py`
  for the full list -- successful login sets a correctly flagged session
  cookie and reaches the protected page; unknown user/wrong password/wrong
  or replayed TOTP code all produce the same generic response; lockout
  after 5 consecutive failures, unlocking again after the configured
  duration; session absolute and idle expiry; logout deletes the session
  server-side (the old cookie value stops working); login rotates
  (never reuses) the session token; CSRF is required and checked on every
  state-changing `/ui` POST; an unauthenticated protected-page request
  redirects (303) to `/ui/login` without ever rendering protected content;
  the security headers are present on every `/ui` response; the stored
  session value is a hash, never the raw cookie value; an agent token
  cannot reach `/ui/` and a UI session cookie cannot reach `/v1/...`; the
  wheel actually contains the templates.
- **Depends on:** P1.3 (storage layer, migrations).
- [x] done -- see `docs/STATUS.md` for the decision, the configuration
  environment variables, and the open points (passkeys; TOTP secrets
  stored in plain text).

### P3.1 -- "The house" view
- **Goal:** overview of all apartments with status.
- **Files:** `fleet/` templates/views (directory not yet created),
  `fleet/app.py`.
- **Section:** 9.
- **Acceptance:** page loads, shows every apartment with heartbeat age and
  open faults; test via FastAPI's HTTP client.
- **Depends on:** P1.3, P2.1.
- [x] done -- see `docs/STATUS.md` for the ordering rule (a derived reading
  of section 9, not a spec quote), what a tile shows and deliberately does
  not (section 6), and the new `fleet/ui_house.py`/
  `Storage.get_house_overview` pieces.

### P3.2 -- "One apartment" view
- **Goal:** detail view of a single apartment.
- **Files:** as P3.1.
- **Section:** 9.
- **Acceptance:** page shows an apartment's history, open faults, and last
  commands.
- **Depends on:** P3.1 (shared templates/navigation).
- [x] done -- see `docs/STATUS.md` for the gap-detection derivation (reuses
  `fleet.alarms.ABSENCE_THRESHOLD`, closing the open point carried since
  P2.1/P2.1b), the new `fleet/ui_apartment.py`/`Storage` read methods, and
  the two open points this package leaves for later (per-device
  battery/signal values -- not in the heartbeat protocol; commands -- a
  later step).

### P3.3 -- "Inventory" view -- **merged into P4.1**
- **Goal (as originally planned):** fourth view for property/apartment/
  device/assignment.
- [x] done -- **built as part of P4.1, not as its own package.** Decided by
  the project owner, 2026-09-26, alongside P4.1's own rework: landlord
  inventory actions are `/ui` forms, not `/v1` endpoints, so this view and
  P4.1's storage/schema foundation are one cohesive piece of work, not two
  packages where the second only reads what the first wrote. See P4.1
  below and `docs/STATUS.md`'s P4.1 section for the "Inventar" view itself
  (`fleet/ui_inventory.py`, `fleet/templates/ui/inventory*.html`).

### P3.4 -- "Tasks" view
- **Goal:** section 9's third view, "what is due": battery rounds, updates,
  unconfirmed faults -- "the list people actually work from." Noted here
  explicitly because this plan's step 3 had so far only ever listed two of
  section 9's three views (P3.1 "the house", P3.2 "one apartment") --
  spotted while writing up P3.0, not a change of scope, section 9 always
  named three.
- **Files:** as P3.1, plus whatever P2.2/P4.x endpoints or storage queries
  a task actually needs (battery-round data from heartbeats already stored,
  update availability, unconfirmed/open faults from `fleet/storage.py`'s
  `events`/`alarms` tables) -- most likely new read-only aggregation
  helpers on `Storage`, not new tables.
- **Section:** 9.
- **Acceptance:** page lists the three task kinds; test checks that a
  battery round, a pending update, and an unconfirmed fault each actually
  show up when the underlying data says they should, and that the page is
  empty when none do (mirroring "whoever has nothing to do sees a quiet
  surface" from P3.1's own goal).
- **Depends on:** P3.1 (shared templates/navigation -- the "Aufgaben" nav
  entry already exists as an inactive placeholder since P3.0, see
  `fleet/templates/ui/base.html`).
- [x] done -- see `docs/STATUS.md` for the three named thresholds (citing
  their own row of section 8's alarm table), the "already on Das Haus"
  exclusion, and the open points (the version-gap reference needs a "current
  release" concept this service does not have yet; no fault-acknowledgement
  mechanism is built).

---

## Step 4 -- Inventory management (section 20)

**Reworked in scope by the project owner, 2026-09-26 (P4.1):** the six
`/v1` endpoints this step's packages were originally going to fill in
(`read_inventory`, `register_device`, `prepare_device`,
`confirm_device_registration`, `replace_device`, `change_device_state`)
have been **removed from `fleet/app.py`** -- landlord inventory actions are
server-rendered `/ui` forms behind the P3.0 login, not agent-reachable
`/v1` endpoints; `/v1` stays a pure agent API (section 4/18.1's token
check, not the P3.0 session/CSRF this now uses). The three rules from
section 20.3 (at most one active device per apartment, a device belongs to
at most one apartment, no release without a confirmed verification code)
still split this step's remaining work the same way; only the endpoint
surface each package touches changed, from `/v1/...` to `/ui/inventory/...`.

### P4.1 -- Inventory foundation + "Inventar" view
- **Goal:** the schema (`properties`, `apartments` extended, `devices`,
  `assignments`, `inventory_audit_log`; migration `0006_inventory.py`),
  `Storage` methods for all of it, and the "Inventar" UI (section 9's
  fourth view, section 20.4 -- **absorbs P3.3**, see that package's own
  entry above): create a property, create an apartment (permanent id,
  charset/uniqueness validated, never editable afterward), register a
  device (initial state always `registered`), edit an apartment's label/
  floor/orientation/heating circuits/state/`pilot_mode` (with a mandatory
  logged reason -- `pilot_mode` is security-relevant, CLAUDE.md principle
  5), list properties/apartments/devices with the "in_storage"/"faulty"
  filters. None of the three section 20.3 rules apply yet (reading and
  plain creation only) except that `apartments.token_hash` becoming
  nullable ("an apartment exists before any device is confirmed") had to
  be proven not to weaken `fleet/auth.py`'s token check.
- **Files:** `fleet/migrations/versions/0006_inventory.py`,
  `fleet/storage.py`, `fleet/ui_inventory.py` (new), `fleet/ui_routes.py`,
  `fleet/templates/ui/{inventory,inventory_apartment_edit}.html`,
  `fleet/static/ui/fleet-ui.css`, `fleet/app.py` (the six old `/v1` stubs
  removed).
- **Section:** 20.1-20.4.
- **Acceptance:** an apartment can be created and its device registered
  and read back via the "Inventar" view; a `NULL` `token_hash` apartment
  still gets 403 on every agent endpoint; the two partial unique indexes
  from section 20.3 (one open assignment per apartment/per device) are
  enforced at the database level, including under concurrent calls; every
  change to assignment/state/token is logged with who/when/why.
- **Depends on:** P1.3.
- [x] done -- see `docs/STATUS.md` for the schema, the migration's legacy-
  row defaults, the NULL-token-hash proof, and the audit log.

### P4.2 -- Prepare device, confirm registration and assign
- **Goal:** UI routes under `/ui/inventory/devices/{id}/prepare` and
  `/ui/inventory/apartments/{id}/confirm-device` (reworked from the former
  `/v1` stubs `prepare_device`/`confirm_device_registration`, same
  rework as P4.1's own), including "no release without a confirmed
  verification code" (20.3) and "a device belongs to at most one
  apartment" on assignment. Device-side registration itself moves to
  P4.2b (Ed25519 + a signed challenge), not built here or in P4.1.
- **Files:** `fleet/ui_routes.py`, `fleet/ui_inventory.py`.
- **Section:** 15.3, 20.3.
- **Acceptance:** test demonstrates both rules negatively (an assignment
  attempt without a confirmed verification code fails; a double assignment
  fails).
- **Depends on:** P4.1.
- [x] done -- built as `GET/POST /ui/inventory/devices/{id}/prepare` and
  `GET /ui/inventory/devices/confirm` + `POST /ui/inventory/devices/{id}
  /confirm` (a devices-list-then-confirm page, not a per-apartment
  `.../apartments/{id}/confirm-device` route as first sketched here -- the
  actual work order asked for a listing of every `reported` device with one
  confirmation form each, which fits the `/devices/...` path this package
  already uses for "prepare", not `/apartments/...`). Registration state
  (`fleet/migrations/versions/0007_device_registrations.py`,
  `Storage.prepare_device`/`record_device_report`/`confirm_device`) and the
  UI are both this package's own work -- see `docs/STATUS.md` for the full
  writeup, the 24h/5-attempts limits, and what P4.2b must still do.

### P4.2b -- Device-side registration (Ed25519 + signed challenge) **SR**
- **Goal:** the device side of section 15.3 step 2/4: a freshly started
  agent generates its own Ed25519 key pair (never leaving the device,
  CLAUDE.md security principle 3), registers with the cloud using the
  one-time registration code (P4.2's `prepare`) and its **public** key via
  a new `/v1/registration/...` endpoint group, and answers a signed
  challenge from the cloud to prove possession of the corresponding
  private key before `Storage.get_current_device_for_apartment`'s
  `public_key_fingerprint` is ever trusted for anything security-relevant.
- **Files:** `agent/` (key generation, challenge response), `fleet/app.py`
  (`/v1/registration/...`, new -- the one exception to "inventory is `/ui`
  only", since this is the agent's own registration, not a landlord
  action), `protocol/registration.py` (if a new field is genuinely needed --
  clear with the project owner first, per CLAUDE.md).
- **Section:** 4, 14, 15.3.
- **Acceptance:** a registration request with a valid code and public key
  succeeds; an unsigned or wrongly-signed challenge response is rejected;
  the private key never appears in any request, response, log line, or
  stored value anywhere in the cloud (CLAUDE.md security principle 3).
- **Depends on:** P4.2.
- **Read back by:** main session (private-key handling, security
  principle 3).
- [x] done -- decided by the project owner (2026-09-26): Ed25519 + a
  signed server challenge, the verification code derived from the public
  key's own fingerprint (`protocol.registration.verification_code_for`).
  Three new agent-facing endpoints (`POST /v1/registration`, `.../{id}
  /challenge`, `.../{id}/token`, `fleet/app.py`), purely additive protocol
  models (`protocol/registration.py`: `RegistrationAccepted`,
  `TokenChallenge`, `TokenRequest`, `TokenIssued`), migration `0008
  _device_registration_tokens.py`. See `docs/STATUS.md` for the full flow,
  the message format, the per-IP throttle (three independent purposes),
  and the open points (token rotation, the agent side itself in P2.3, TLS
  pinning on the agent).

### P4.3 -- Replace device, change state
- **Goal:** UI routes under `/ui/inventory/apartments/{id}/replace-device`
  and `/ui/inventory/devices/{id}/state` (reworked from the former `/v1`
  stubs `replace_device`/`change_device_state`, same rework as P4.1's
  own), including "at most one active device per apartment" and
  `DeviceLifecycle` transition checks (P4.1 deliberately does not enforce
  transitions -- "not part of this package, except that registration sets
  `registered`").
- **Files:** `fleet/ui_routes.py`, `fleet/ui_inventory.py`.
- **Section:** 15 (device swap), 20.3.
- **Acceptance:** test demonstrates: a second active device for the same
  apartment is rejected; a state transition in `DeviceLifecycle` outside
  the seven allowed values is already excluded by Pydantic (only the
  transition itself needs checking here, e.g. "decommissioned" not
  reversible, if the specification requires it -- otherwise an open point
  for `STATUS.md`, not an invention of our own).
- **Depends on:** P4.1.
- **Parallel to:** P4.2.
- [x] done -- see `docs/STATUS.md` for the derived transition table
  (`fleet/device_lifecycle.py`, five manual transitions out of the 49
  possible pairs), the "Gerät ausbauen/tauschen" flow
  (`Storage.remove_device`: closes the assignment, sets the removed
  device's state, revokes the apartment's token, all in one transaction),
  and the P4.2 integration notes (the "prepare" link, decommissioning must
  also invalidate a pending registration once P4.2's table exists).

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
