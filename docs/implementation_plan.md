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
- **Split by the project owner, 2026-09-26 (P5.0):** the transport half of
  this package -- TLS pinning, device registration (Ed25519 + signed
  challenge, P4.2b's device-side counterpart), token storage, and
  `send_heartbeat` including catch-up buffering -- does **not** depend on
  thermoctl's missing `/api/v1/health` and moved to P5.0 below, at the top
  of step 5 (it needs no `GET /v1/commands` machinery either, but step 5 is
  where the remaining, still-**SR**, agent packages live). What stays here,
  still deferred: `collect_heartbeat` itself (reading thermoctl) -- no
  stub or invented behaviour for it exists anywhere in this repository.
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
  exclusion (superseded by P3.4a below), and the open points (the
  version-gap reference needs a "current release" concept this service does
  not have yet; no fault-acknowledgement mechanism is built).
- [x] **P3.4a** (2026-09-26, project owner decision) -- keep tasks of silent
  apartments, marked stale: an apartment with an open "not reporting" alarm
  no longer excluded from every group wholesale; instead each qualifying
  row stays, carrying a `stale` flag plus a rendered German hint (heartbeat
  age and alarm-since time, e.g. "... Wohnung meldet sich nicht") -- text,
  not colour alone. A never-reported apartment stays excluded, unchanged.
  See `docs/STATUS.md`'s P3.4 section for the full reasoning.

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

### P5.0 -- Agent transport: pinned HTTPS client, device registration, token
storage, heartbeat sending **SR**
- **Goal:** the part of P2.3 that does not depend on thermoctl's missing
  `/api/v1/health` (project owner, 2026-09-26): a pinned, always-verifying
  HTTPS client (section 4 -- certificate verification never disabled, plus
  fingerprint pinning as a second barrier); the device side of registration
  (section 15.3 steps 1-3, Ed25519 + signed challenge exactly as P4.2b
  defines it fleet-side); private-key and agent-token storage (mode 0600,
  the private key never transmitted or logged, CLAUDE.md security
  principle 3); and `send_heartbeat` itself, including local buffering and
  one-batch catch-up delivery after an outage (section 5). `collect_heartbeat`
  (reading thermoctl) stays deferred -- see P2.3 above -- and nothing here
  is wired into a running main loop that would need it.
- **Files:** `agent/transport.py` (pinned client), `agent/registration.py`
  (registration client, key/token storage, local status file),
  `agent/heartbeat_sender.py` (send + buffer), `agent/__main__.py` (`register`
  subcommand), `agent/loop.py` (docstring updates only -- `collect_heartbeat`
  and the main loop itself untouched), `pyproject.toml` (`cryptography`,
  `httpcore` added to the `agent` extra), `fleet/ui_routes.py` (a
  fingerprint-format comment only, no behaviour change).
- **Section:** 3, 4, 5, 10, 14, 15.3, 18.1, 18.2, 19.5, 23.2.
- **Acceptance:** full registration end-to-end against a real fleet app
  over real TLS with a throwaway test CA (prepare via storage -> agent
  registers -> confirm via storage -> agent polls `202` then gets the
  token -> token file 0600 -> `send_heartbeat` -> `204`); a certificate
  pin mismatch aborts the TLS handshake before any request byte (including
  the bearer token) is ever sent, proven with a recording TLS server; an
  untrusted CA is refused even with a matching pin (verification stays on);
  `http://` URLs are refused before any connection is attempted; the
  private key file is mode 0600 and never appears in a request body, a log
  line, or the stored database; the heartbeat buffer is capped at
  `protocol.heartbeat.MAX_CATCH_UP_HEARTBEATS` (240, oldest dropped first)
  and flushed in one `POST /v1/heartbeats` batch on the next successful
  contact; a `401`/`403` surfaces as an error instead of buffering forever;
  an existing token means no re-registration (idempotent).
- **Depends on:** P4.2b (the fleet-side registration endpoints this
  package's client talks to).
- **Read back by:** main session (pinned transport, private-key handling,
  security principle 3).
- [x] done -- see `docs/STATUS.md` for the pin format (`sha256:<hex>`),
  exactly what the pin check guarantees relative to bytes on the wire, file
  locations and modes, the poll interval, and the buffer rules.

### P5.1 -- SSE channel `GET /v1/commands`
- **Goal:** implement `fleet/app.py::commands_stream` and the agent's own
  command channel: SSE delivery, `Last-Event-ID` reconnection, 60-second
  poll fallback on an interrupted connection.
- **Files:** `fleet/app.py` (`commands_stream`, `receive_command_result`),
  `fleet/storage.py` (`commands` table, `create_command`,
  `pending_commands`, `record_command_result`),
  `fleet/migrations/versions/0009_commands.py`, `protocol/commands.py`
  (`Command.protocol_version`), `protocol/version.py` (`PROTOCOL_VERSION`
  bumped to 3), `agent/commands_channel.py` (new -- `receive_commands`,
  `report_result`), `agent/loop.py` (docstring updates only, pointing at
  the new module, mirroring P5.0's `send_heartbeat` placeholder).
- **Section:** 3, 7, 18.2.
- **Acceptance:** test holds an SSE connection, interrupts it, confirms
  the 60-second poll as the fallback, then resumes the stream -- done
  against a real fleet app over real TLS, the server actually stopped and
  restarted mid-test (`tests/test_agent_commands_channel.py`). A malformed
  event or an unknown `CommandType` is surfaced as a rejected item, never
  executed; a newer `protocol_version` than this agent understands is
  rejected the same way (section 18.2).
- **Depends on:** P1.1.
- [x] done -- see `docs/STATUS.md`'s P5.1 section for the SSE event
  format, `Last-Event-ID`/`wait=0` semantics, the fallback polling cadence,
  the result endpoint's exact status-code mapping, and the P5.0 transport
  fix (`agent/transport.py`'s pinned client no longer buffers a response
  body eagerly) this package needed to hold a real streaming connection
  open at all.

### P5.1b -- Stage-1 command buttons with confirmation in "Eine Wohnung"
- **Goal:** turn P3.2's static "commands not yet available" note into one
  button per `CommandType` value, generated from the enum, each behind a
  two-step confirmation page with a mandatory reason, plus a "Befehle"
  history list on the apartment page.
- **Files:** `fleet/storage.py` (`create_command` gains `reason`, an audit
  row; `has_pending_identical_command`, `list_commands_for_apartment`,
  `DOUBLE_SUBMIT_WINDOW`), `fleet/ui_apartment.py` (`COMMAND_TYPE_LABELS`,
  `available_commands`, `CommandDisplay`, `build_command_history`,
  `ApartmentDetail.retired`/`available_commands`/`commands`),
  `fleet/ui_routes.py` (`command_confirm_form`, `command_confirm_submit`),
  `fleet/templates/ui/command_confirm.html` (new),
  `fleet/templates/ui/apartment.html`, `fleet/static/ui/fleet-ui.css`.
  `protocol/`, `agent/`, `watchdog/`, `fleet/auth.py`, `fleet/ui_auth.py`
  untouched; no migration (reuses `commands` and `inventory_audit_log`,
  both already existing).
- **Section:** 9, 20.3 (audit: who/when/why).
- **Acceptance:** buttons exactly match `CommandType`
  (`COMMAND_TYPE_LABELS`/`available_commands` tested for exact enum
  coverage); GET confirmation names apartment and command, POST (CSRF,
  `require_ui_user`) is the only caller of `Storage.create_command`;
  retired apartments show no buttons and refuse the POST; double-submit
  protection (`has_pending_identical_command`, a 10 s window) verified at
  storage and HTTP level; command history shows status derived from stored
  fields, duration, and escaped/truncated `error_text`; another
  apartment's commands never shown; an unknown command in the URL is a 404
  that never reaches storage.
- **Depends on:** P5.1.
- [x] done -- see `docs/STATUS.md`'s P5.1b section for the double-submit
  design decision and the full verification output.

### P5.2 -- Command execution in the agent **SR**
- [x] done -- see `docs/STATUS.md`'s P5.2 section (including its provenance
  note: a first, uncommitted draft of this package came from an interrupted
  Codex run and was reviewed and corrected here, not used as-is).
- Implemented: a closed handler mapping covering exactly the five stage-1
  `CommandType` values, persisted last-200 execution ids (at-most-once
  across restarts), expiry/newer-protocol-version handling, a bounded local
  log, and `python -m agent run` (new CLI subcommand).
- `agent_restart` is genuinely executed: reports its result before an
  injectable clean exit, refuses a pending swap (`desired != proven`) and
  fails closed on a missing/incomplete watchdog state file (cannot
  conclusively rule out a pending swap). Duplicate ids are never
  re-executed and never re-reported (no synthetic second result).
- Per-command follow-ups, each an honest failed result naming the package
  that will replace it: `report_now` needs P2.3 heartbeat acquisition;
  `fetch_logs` needed P5.3a masking/upload (done, see below);
  `diagnostic_bundle` needs P5.3b; `backup_now` needs P5.5.
- LED `cloud_contact` follows the command channel's own already-existing
  WARNING log (no change to `agent/commands_channel.py`); `fault`/`control`
  stay honestly unknown (not a fabricated "no fault") via a deliberately
  stale timestamp, reusing `cmd/thermoctl-leds`'s existing staleness
  fallback rather than inventing a new state -- watchdog code unchanged.
- **Tests:** unit-level execution/log/state tests, CLI wiring tests, and
  end-to-end tests against the real fleet app over real TLS -- all
  verified green in this session (fresh venv, see STATUS's verbatim
  verification output).
- **Depends on:** P5.1. **Read back by:** main session (principle 5).

### P5.3 -- Result reporting and `create_diagnostic_bundle`

Split, 2026-09-27 (project owner), into P5.3a (`fetch_logs`, done) and P5.3b
(`diagnostic_bundle`, still open) -- the two commands need unrelated
machinery (an on-device allowlist filter vs. end-to-end encryption) and
were never a single unit of work to begin with; see
`docs/specification.md` 21.5's own "Decided afterward" paragraph for why
they must also stay conceptually separate, not just separately scheduled.

#### P5.3a -- `fetch_logs` with an on-device allowlist filter **SR** -- done
- **Goal:** complete `fetch_logs` (stage 1): read the thermoctl container's
  own log, filter it through an on-device allowlist (never a denylist,
  never cloud-side filtering), upload the result, and show it in "Eine
  Wohnung". **SR** because of the filtering itself (section 21.5, project
  owner decision 2026-09-27).
- [x] done -- see `docs/STATUS.md`'s P5.3a section for the full design
  (allowlist shapes, placeholders, retention) and verification output.
- **Files:** `agent/log_filter.py` (new), `agent/loop.py`
  (`_handle_fetch_logs`, `read_container_log_lines`), `protocol/commands.py`
  (`LogExcerpt`, `PROTOCOL_VERSION` bumped to 4), `fleet/app.py`
  (`POST /v1/commands/{id}/logs`, retention loop), `fleet/storage.py`
  (`command_log_excerpts` table), `fleet/ui_apartment.py`/
  `fleet/templates/ui/apartment.html` (display).
- **Section:** 6, 7, 21.5.
- **Acceptance:** a test feeding a log with a tenant name, °C values, a
  token, a setpoint, and a free-text note demonstrates none of it appears
  in the output, with an accurate dropped-line count.
- **Depends on:** P5.2. **Read back by:** main session (filtering is
  security-relevant).

#### P5.3b -- `create_diagnostic_bundle`, end-to-end encrypted **SR** -- open
- **Goal:** complete `create_diagnostic_bundle` (stage 1): logs of the four
  services, versions/digests, container states, memory/disk usage, Zigbee
  network state, the last control decisions -- packaged and **end-to-end
  encrypted** (a key the cloud does not possess, mirroring the
  operational-data backup's own section-15.1 treatment), delivered once,
  never accumulated in the cloud's running storage the way `fetch_logs`'s
  own retention window does. **Must not** carry a *series* of control
  decisions or heat-demand readings over time (section 6, section 21.5's
  own "Decided afterward" paragraph) -- a bundle is a snapshot over a few
  hours, not a channel.
- **Files:** `agent/loop.py` (`create_diagnostic_bundle`), reusing
  `agent/encryption.py` (P5.5a, done) directly rather than duplicating its
  own recipients-file handling -- the project owner's 2026-09-26/27 backup
  decision explicitly asks for the same encryption mechanism to be "reused
  later by the encrypted diagnostic bundle", and `load_recipients`/
  `encrypt_stream` are already general-purpose (a recipients file path, a
  byte stream in, a byte stream out), not backup-specific, precisely for
  this.
- **Section:** 15.1, 21.5.
- **Acceptance:** test demonstrates the cloud never sees plaintext content,
  and that a series of bundles cannot be used to reconstruct a heat-demand
  timeline (only ever one bundle at a time, no accumulation).
- **Depends on:** P5.2, P5.5a (done -- `agent/encryption.py`).
- **Read back by:** main session (encryption and the "never a series"
  boundary are both security-relevant).

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

### P5.5a -- Backup creation and upload **SR**
- [x] done -- see `docs/STATUS.md`'s P5.5a section.
- **Goal:** implement `agent/loop.py::create_backup` for both kinds of
  backup, encrypting the operational-data backup (real `age` format, two
  recipients, `agent/encryption.py`) before it ever touches an upload
  buffer (security principle 4); upload both kinds to new fleet endpoints
  (`POST /v1/backups`), storing operational data only as the opaque
  encrypted block; retention (section 15.2, 14 daily + 8 weekly); UI list
  and download, with the ready-made `age -d ...` command next to it.
- **Files:** `agent/loop.py`, `agent/encryption.py`, `fleet/app.py`,
  `fleet/backup_storage.py`, `fleet/backup_retention.py`,
  `fleet/storage.py`, `fleet/ui_apartment.py`, `fleet/ui_routes.py`,
  `protocol/backups.py`, `image/common/agent-compose.yml`.
- **Section:** 15.1, 15.2, 15.3, 19.5.
- **Acceptance:** test demonstrates that an operational-data backup is not
  readable without a valid local key (real encryption/decryption, no
  mock); device configuration stays plain text, test demonstrates the
  separate handling; fewer than two recipients (or an invalid/missing/
  unsafe recipients file) refuses the backup, nothing uploaded, no
  plaintext written anywhere; the fleet rejects a non-age operational
  upload; retention keeps exactly the documented daily/weekly window;
  download requires login; end-to-end `backup_now` against the real fleet
  app over TLS.
- **Depends on:** nothing from step 5, can start in parallel with
  P5.1-P5.4.
- **Read back by:** main session (encryption, security principle 4).

### P5.5b -- Restore -- **open, not started**
- **Goal:** section 15.2/15.3 step 4's counterpart to P5.5a: "The agent
  fetches the device configuration and, on a swap, the encrypted
  operational data. The landlord enters the decryption key once in the
  fleet UI; it is only passed through, never stored." No stub exists yet
  for either the fleet-side "hand the device its backups on
  (re-)assignment" flow or the agent-side "receive and unpack" flow.
- **Files:** likely `fleet/ui_routes.py`/`fleet/app.py` (a new endpoint
  the newly assigned device fetches from) and `agent/loop.py` (unpacking,
  never storing the passed-through key).
- **Section:** 15.2, 15.3 step 4.
- **Depends on:** P5.5a (this package) -- reuses `protocol.backups`,
  `fleet.backup_storage`, and the same age recipients/identity concept.
- **Read back by:** main session (security principle 3: the landlord's
  decryption key must only ever be passed through, never stored).

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
- [x] done -- `AgentStopped`/`StartDigest`/`AwaitHealthReport`/
  `RollBackToProven` implemented against a new `Runtime` interface
  (`watchdog/runtime.go`), addressed only via `os/exec`, never a library --
  `go.mod` still has no `require`. `Reconcile` (new) ties them into one
  pass and returns an `Outcome` for P5.7. Production code (seven files) at
  **299** statement lines (was 195), `go vet`/`go test` clean (41 tests),
  `check_contract.sh` passing, `gofmt -l .` empty.
- [x] **cross-review fixes** (see `docs/STATUS.md` for the full account) --
  R1/R5: `Start` no longer runs a digest directly; it tags locally, then
  re-applies a fixed compose file shipped with the image
  (`image/common/agent-compose.yml`, `pull_policy: never`, `restart:
  on-failure`) with `--pull never`, never sent by the cloud. New
  `-runtime-compose`/`-runtime-repo` flags, container name now a fixed
  constant instead of a flag. R4: digests validated
  (`^sha256:[0-9a-f]{64}$`) before reaching argv. R2: `AwaitHealthReport`'s
  deadline is anchored to the state file's `since`, not to whenever the
  call started -- `Reconcile` now resumes the await/rollback path for an
  already-running desired digest that has not yet proved itself, instead
  of treating "running" as "done" forever. R3: `runtime.go` now has argv
  tests, via an `execCommand` package variable tests swap for a recording
  fake. Deeper finding (main session): `desired`/`proven` are *registry
  manifest* digests, not local image IDs -- `Status` now resolves the
  running container's manifest digest through `RepoDigests`, matched
  against `-runtime-repo` (contract note for P5.4: the agent must pull the
  agent image by digest, or this can never match). `AgentStopped` and
  `StartDigest` retired as separate functions (both now dead weight once
  `Reconcile` needed the same information in one `Status` call) to make
  room under the line budget -- their capabilities are unchanged, exercised
  by `Reconcile`'s own tests now. **299** statement lines still (1 line of
  headroom), 46 tests, `tools/check_image_config.py` gained
  `check_agent_compose_file`.

### P5.7 -- Wire up the status display (section 23)
- **Goal:** connect `watchdog/leds.go` (`LedSetPattern`) to the states from
  P5.6: which watchdog state triggers which pattern from section 23.2.
- **Files:** `watchdog/watch.go`, `watchdog/leds.go`.
- **Section:** 23.
- **Acceptance:** test demonstrates the correct pattern per state; the
  line count stays under the 300-line production-code limit -- P5.6 leaves
  only 1 line of headroom (299/300 after its cross-review fixes), so this
  package will likely need to trim elsewhere before it can add anything;
  `linefile.go`'s `openAndParse` and P5.6's own retiring of two
  redundant functions are the precedents to follow first.
- **Depends on:** P5.6.
- [x] done, **as a separate program, not inside the watchdog** -- decision
  by the project owner, 2026-09-26 (see `docs/specification.md` section
  23's own "Decided afterward" paragraph and `docs/STATUS.md`'s P5.7
  entry for the full account): `watchdog/leds.go`/`leds_test.go` moved
  (not duplicated) into `watchdog/internal/ledsysfs`, the watchdog's own
  six production files went from 299 to **280** statement lines by that
  removal alone, and a new, separate `go build` target
  `watchdog/cmd/thermoctl-leds/` (288 statement lines, plus
  `internal/ledsysfs`'s 65 -- neither counted against the watchdog's own
  budget) reads the watchdog's state/health files, P5.0's registration
  status file, and a new agent-written status file
  (`agent.loop.report_led_status`, new) directly from disk, with its own
  precedence and staleness rules (documented and tested, `decide_test.go`
  covers every state of section 23.2's two tables). Same module, same
  `go.mod` (still no `require`), own systemd unit, own CI build
  (`amd64`/`arm64`, static, checksummed) and own contract-test extension
  (`watchdog/check_contract.sh`). `go vet`/`go test -count=1 ./...` clean
  across all three watchdog-module packages, `gofmt -l .` empty,
  `tools/check_image_config.py` gained `check_leds_unit`.

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
