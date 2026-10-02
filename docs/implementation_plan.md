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

### P5.1c -- SSE resume survives a fleet database restore
- **Goal:** fix a defect found during the P5.E end-to-end run
  (2026-09-28): after the fleet database is reset or restored from an
  older backup, `commands.id` (the SSE stream's own global sequence
  counter, what `Last-Event-ID` resumes from) starts over, so a reused
  sequence number can make a resuming agent silently skip a brand-new
  command instead of receiving it.
- **Files:** `fleet/migrations/versions/0012_fleet_epoch.py` (new --
  one-row `fleet_epoch` table), `fleet/storage.py`
  (`FleetEpochRecord`, `Storage.get_epoch`, `Storage.rotate_epoch`),
  `fleet/app.py` (`_last_event_id`, `_stream_command_events`,
  `commands_stream` -- SSE event id becomes `<epoch>.<sequence>`),
  `fleet/admin.py` (`rotate-epoch` CLI subcommand), `agent/commands_channel.py`
  (`_read_last_event_id` bounded in length/charset before ever being sent
  in a header). `protocol/` and `PROTOCOL_VERSION` untouched -- the SSE id
  is transport metadata, never a wire model field.
- **Section:** 3, 7.
- **Acceptance:** epoch created once by the migration's own data insert,
  stable across restarts and across separate `Storage` instances; SSE ids
  carry it; a `Last-Event-ID` with the current epoch and the apartment's
  own sequence resumes correctly (`Storage.pending_commands`'s existing
  membership check, unchanged); the reproduction -- persist a bookmark,
  simulate a restore (a brand-new database, its own fresh epoch, its own
  sequence counter starting back at 1) -- delivers the new, reused-sequence
  command instead of skipping it
  (`test_commands_stream_sse_survives_a_simulated_restore_new_command_not_skipped`);
  a bare pre-P5.1c integer id, a wrong-epoch id, and garbage/header-
  injection attempts all fall back to `0`, never a 500; `rotate-epoch`
  replaces the stored epoch and is visible from a separate `Storage`
  instance; migration 0012 up/down and `compare_metadata` clean. Agent
  side: the persisted value round-trips opaquely (never parsed as an
  integer), and an invalid persisted value (too long, wrong charset, a
  CR/LF header-injection attempt) is never sent, treated exactly like no
  bookmark at all.
- **Operator note:** rotating the epoch is a manual step, not automatic --
  a freshly migrated database gets its own epoch for free, but a database
  *restored* from an existing backup file also restores whatever epoch was
  in that backup verbatim (`fleet_epoch` is an ordinary table). **After
  restoring a backup, run `python -m fleet.admin rotate-epoch` once**, so
  every agent's already-persisted `Last-Event-ID` stops matching and
  resumes from `0` -- safe, since redelivery of still-pending commands is
  always harmless, unlike the silent skip this package fixes.
- **Depends on:** P5.1.
- [x] done -- see `docs/STATUS.md`'s P5.1c section for the full reasoning
  and verification output.

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
  `diagnostic_bundle` needed P5.3b end-to-end encryption (done, see below);
  `backup_now` needs P5.5.
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

#### P5.3b -- `create_diagnostic_bundle`, end-to-end encrypted **SR** -- done
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
- [x] done -- see `docs/STATUS.md`'s P5.3b section for the full design
  (bundle contents, size bounds, retention) and verification output.
- **Files:** `agent/loop.py` (`create_diagnostic_bundle`, `_handle_diagnostic_bundle`,
  `upload_diagnostic_bundle`, `read_container_log_window`,
  `read_container_state`), reusing `agent/encryption.py` (P5.5a, done)
  directly rather than duplicating its own recipients-file handling -- the
  project owner's 2026-09-26/27 backup decision explicitly asks for the
  same encryption mechanism to be "reused later by the encrypted
  diagnostic bundle", and `load_recipients`/`encrypt_stream` are already
  general-purpose (a recipients file path, a byte stream in, a byte stream
  out), not backup-specific, precisely for this. `protocol/diagnostics.py`
  (new, `DiagnosticBundleUploadAccepted`, `PROTOCOL_VERSION` bumped to 6).
  `fleet/app.py` (`POST /v1/commands/{id}/bundle`, retention loop),
  `fleet/upload_streaming.py` (new, factored out of P5.5a's own
  `upload_backup` so both endpoints share the streaming-cap/age-plausibility
  logic), `fleet/bundle_storage.py` (new), `fleet/storage.py`
  (`diagnostic_bundles` table, migration `0013_diagnostic_bundles.py`,
  re-chained onto P5.1c's parallel `0012_fleet_epoch.py` at merge time),
  `fleet/ui_apartment.py`/`fleet/ui_routes.py`/`fleet/templates/ui/
  apartment.html` (shown next to its command in "Befehle", not a separate
  list).
- **Section:** 15.1, 21.5.
- **Acceptance:** test demonstrates the cloud never sees plaintext content
  (real crypto throughout, marker-scanning tests), and that a bundle stays
  a one-off snapshot, never accumulated as a series (one bundle per
  command id, enforced at the database level; 14-day retention deletes the
  fleet's own stored copy the same way `fetch_logs`'s excerpts expire).
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
  15 minutes triggers a rollback. **Owner decision 2026-09-28 (section 13,
  "Decided afterward"):** fail-closed pre-check (unreadable thermoctl health
  or outdoor temperature -> reject) **and** `pilot_mode` required at the
  target; both rejection paths tested; no new `CommandType`. STATUS.md must
  record P5.4 as "done but inactive" with both conditions.
- **Depends on:** P5.1 (the desired state arrives over the same channel as
  commands, see section 13).
- [x] done, **agent side only** -- see `docs/STATUS.md`'s own P5.4 section
  for the full design (`agent/sources.py`'s exact-match source check, the
  Docker Engine API pull/verify/swap/rollback path, the restart-safe
  pending-swap record) and verification output. **Stays inactive**, per
  the owner decision above: fetching the desired state from the fleet over
  SSE, and any fleet-side storage/UI for it, are **P5.4b**, not started.
- **Read back by:** main session (the digest check is the central
  safeguard from security principle 2).

### P5.4b -- Desired-state delivery, fleet side + agent wiring **SR**
- [x] done -- see `docs/STATUS.md`'s own P5.4b section for the full design
  and verification output.
- **Goal:** fleet-side per-apartment desired-state storage with full
  history/audit; a per-apartment UI form (version + digest per service,
  the update window), with a confirmation step and server-side digest
  validation; delivery over the existing SSE channel as a separate
  `desired_state` event (not a `Command`, not a `CommandType` value);
  agent-side wiring that validates the event, ignores a stale revision,
  calls `agent.loop.reconcile_desired_state` with the delivered
  `pilot_mode`, and reports the outcome back to the fleet.
- **Files:** `protocol/desired_state.py` (`DesiredStateEvent`,
  `DesiredStateOutcomeReport`), `protocol/version.py` (8),
  `fleet/storage.py` (`DesiredStateRecord`, `DesiredStateOutcomeRecord`),
  `fleet/migrations/versions/0015_desired_state.py`, `fleet/app.py` (SSE
  emission, `POST /v1/desired-state/result`), `fleet/desired_state_sources.py`
  (new, display-only), `fleet/ui_apartment.py`/`fleet/ui_routes.py`
  (the two-step form), `fleet/templates/ui/desired_state_edit.html`/
  `desired_state_confirm.html`/`apartment.html`, `agent/commands_channel.py`
  (`DesiredStateReceived`), `agent/loop.py` (`run`'s own wiring),
  `agent/__main__.py`.
- **Section:** 13.
- **Acceptance:** `CommandType` unchanged (exact-set test); no
  source/registry field the fleet can set that the agent trusts (the form
  has no such field, and a test pins `fleet.desired_state_sources
  .DISPLAY_SOURCES` against `agent.sources.ALLOWED_SOURCES`); a stale
  revision is ignored; `pilot_mode=False` rejects, tested end to end over
  the real SSE channel with the real TLS harness; CSRF/login required;
  an invalid digest is refused server-side; history/audit is written.
- **Depends on:** P5.4 (agent-side `reconcile_desired_state`), P5.1c (the
  SSE channel and its epoch-prefixed ids).
- **Read back by:** main session (desired-state delivery and the closed
  command list interact directly, security principles 1 and 5).

### P5.4c -- Rollout queue, fleet side **SR**
- [x] done -- see `docs/STATUS.md`'s own P5.4c section for the full design
  and verification output.
- **Goal:** the pilot-first, 48-hour-staggered rollout queue that stops at
  the first apartment that does not come back healthy (section 13, "Rules
  for the rollout") -- layered on top of P5.4b's own per-apartment
  desired-state storage, which only ever delivers *one* apartment's
  desired state at a time by construction ("the fleet service knows no
  'for all'") and does not sequence *across* apartments. A rollout targets
  exactly one service family (never thermoctl and Zigbee2MQTT mixed, one
  target release), an ordered apartment queue with pilot apartments
  automatically first, a background worker that advances one apartment at
  a time and stops the rollout at the first failure or timeout, and a UI
  list/detail page with resume/cancel.
- **Files:** `fleet/storage.py` (`RolloutRecord`/`RolloutApartmentRecord`),
  `fleet/migrations/versions/0016_rollouts.py`, `fleet/rollout.py` (new,
  the worker logic), `fleet/ui_rollout.py` (new), `fleet/ui_routes.py`,
  `fleet/app.py` (the new lifespan background task), `fleet/templates/ui
  /rollout_list.html`/`rollout_new.html`/`rollout_confirm.html`
  /`rollout_detail.html` (new), `fleet/templates/ui/base.html`.
- **Section:** 13.
- **Acceptance:** pilot apartment(s) always sequenced first; the rest
  gated on 48h (configurable) after every pilot converged; never more
  than one apartment in progress per rollout; never two rollouts touching
  the same apartment concurrently; a stop at the first reported failure
  or unmet timeout; resume/cancel only via explicit, audited UI action;
  mixing thermoctl and Zigbee2MQTT in one rollout structurally refused;
  CSRF/login required; the worker is idempotent across a restart. No
  protocol change -- `protocol.desired_state.DesiredState` reused
  unchanged, `PROTOCOL_VERSION` stays 8. **Still inactive**, unchanged
  from P5.4/P5.4b's own gate (fail-closed pre-check, `pilot_mode`).
- **Depends on:** P5.4b (`Storage.create_desired_state_revision`, the
  desired-state delivery/outcome-report path this package reuses
  unchanged).
- **Read back by:** main session (a background worker sequencing
  desired-state changes across apartments interacts directly with
  security principles 1 and 5).
### P5.4d -- Pre-activation points from the P5.4b merge **SR**
- [x] done -- see `docs/STATUS.md`'s own P5.4d section for the full design
  and verification output.
- **Goal:** close the four points the P5.4b merge review flagged as
  required before desired-state reconciliation may ever be activated: one
  agent-wide lock shared by every container/backup operation; a
  non-blocking immediate trigger (the command thread must not wait on a
  reconcile attempt); a cheap, read-only drift re-check once a revision
  has converged, with a guard against retrying a digest already rolled
  back as unhealthy; and a real concurrency test proving `attempt()` is
  strictly serialized.
- **Files:** `agent/loop.py` (`ExecutionContext.agent_lock`,
  `_handle_backup_now`, `run_daily_backup_scheduler`,
  `_handle_agent_restart`, `AGENT_RESTART_LOCK_TIMEOUT_S`,
  `_handle_desired_state_received`, `run_desired_state_reconcile_loop`,
  `_DesiredStateReconciler`, `_FailedRollback`/`_load_failed_rollback`/
  `_save_failed_rollback`, `ReconcileOutcome.rolled_back_unhealthy`,
  `DEFAULT_DESIRED_STATE_FAILED_ROLLBACK_FILE`), `agent/__main__.py`.
- **Section:** 13 (the pre-activation gate itself, section 13's "Decided
  afterward", is unchanged by this package).
- **Acceptance:** a command arriving during a fake, blocking reconcile is
  executed (and its result reported) before its own expiry; two
  concurrent `attempt()` calls are strictly serialized; the reconciler,
  `backup_now`, the daily scheduler, and `agent_restart` all share one
  lock (proven by a held-lock test blocking each); `agent_restart` fails
  fast and clearly, never silently, if it cannot acquire the lock within a
  bounded timeout; a converged revision's drift re-check makes zero
  Docker calls while `pilot_mode` is false; a digest already rolled back
  as unhealthy for the held revision is never retried automatically,
  reported once, and only cleared by a new revision.
- **Depends on:** P5.4, P5.4b (this package only closes their own
  documented open points, changes no security boundary).
- **Explicitly out of scope:** P5.4c's rollout queue (done separately,
  see P5.4c above); activation
  itself still requires a real thermoctl health/outdoor-temperature
  reader and `pilot_mode` set per apartment (unchanged from P5.4/P5.4b).
- **Read back by:** main session (the shared lock and the non-blocking
  trigger both touch CLAUDE.md security principles 2 and 5 directly).

### P5.4e -- Rollout test apartment decoupled from `pilot_mode`
- [x] done -- see `docs/STATUS.md`'s own P5.4e section for the full design
  and verification output.
- **Goal:** owner decision (section 13, "Decided afterward", 2026-10-02):
  a rollout no longer requires any selected apartment to carry the
  device-side `pilot_mode` flag. The rollout's own test apartment is now
  chosen within the rollout itself -- the landlord may mark exactly one
  apartment of the create form's list as the test apartment, or else the
  first apartment of the list is used. The 48-hour stagger gate and
  everything else about the queue (one apartment at a time, stop at the
  first failure/timeout) stays unchanged; only the *selection* of which
  apartment goes first changes.
- **Files:** `fleet/storage.py` (`Storage.create_rollout`'s new
  `test_apartment_id` parameter; `RolloutRecord`/`RolloutApartmentRecord`
  docstrings), `fleet/rollout.py` (docstring/comment updates only, no
  behaviour change -- `is_pilot` already generalized to "the rollout's
  own test apartment"), `fleet/ui_routes.py` (`rollout_new_submit`/
  `rollout_confirm_submit`), `fleet/templates/ui/rollout_new.html`
  (per-apartment radio button), `fleet/templates/ui/rollout_confirm.html`
  (hidden field, "(Testwohnung)" tag), `fleet/templates/ui
  /rollout_detail.html`/`rollout_list.html` (German text), `tests/test
  _storage_rollouts.py`, `tests/test_rollout_worker.py`, `tests/test_ui
  _rollout.py`.
- **No migration.** `RolloutApartmentRecord.is_pilot` is reused unchanged
  -- its meaning since this package is "this rollout's own test
  apartment", decoupled from `ApartmentRecord.pilot_mode`, but the column
  itself needed no schema change.
- **Section:** 13.
- **Acceptance:** no apartment marked -> the first apartment of the
  submitted list is the test apartment and is started first, with the
  48h gate applying to the rest; an apartment explicitly marked (not
  first in the list) goes first instead; a single-apartment rollout
  works trivially; `pilot_mode` flags are irrelevant to rollout ordering
  (tested directly: a `pilot_mode=True` apartment listed first is *not*
  started first unless also marked); the former "at least one selected
  apartment must carry `pilot_mode=True`" refusal is removed (tested:
  a rollout across apartments with no `pilot_mode` at all now succeeds);
  the create/confirm UI shows which apartment is the test apartment.
- **Depends on:** P5.4c (the rollout queue this package only changes test
  apartment selection within).
- **Read back by:** main session (changes `Storage.create_rollout`'s own
  validation, shared with P6.1/P6.2 work on `fleet/storage.py` in
  parallel worktrees -- confined to the rollout-specific code paths only).

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

### P5.5b -- Restore **SR**
- [x] done -- see `docs/STATUS.md`'s P5.5b section for the full design.
- **Goal:** section 15.2/15.3 step 4's counterpart to P5.5a: "The agent
  fetches the device configuration and, on a swap, the encrypted
  operational data. The landlord enters the decryption key once in the
  fleet UI; it is only passed through, never stored." No stub exists yet
  for either the fleet-side "hand the device its backups on
  (re-)assignment" flow or the agent-side "receive and unpack" flow.
- **Files:** `protocol/version.py`, `protocol/registration.py` (new
  `age_recipient` field), `protocol/restore.py` (new), `fleet/app.py`
  (three new endpoints), `fleet/age_key_block.py` (new), `fleet/storage.py`
  (`devices.age_recipient`, new `pending_restores` table),
  `fleet/migrations/versions/0014_restore.py` (new), `fleet/ui_routes.py`/
  `fleet/ui_apartment.py` (the "Wiederherstellen" form, incl. the vendored
  script's own sha256 shown next to it), `fleet/templates/ui/apartment.html`,
  `fleet/static/ui/vendor/age-encryption.vendor.js` (new, vendored),
  `fleet/static/ui/restore_form.js` (new), `fleet/restore_vendor.py` (new),
  `agent/age_identity.py` (new), `agent/restore.py` (new -- **stages** into
  the agent's own directory, never writes the live thermoctl/Zigbee2MQTT
  data, see P5.5c below), `agent/registration.py`, `agent/loop.py`,
  `agent/__main__.py`, `agent/safe_io.py` (new `write_bytes_safe`).
- **Section:** 15.2, 15.3 step 4 and its two "Decided afterward" paragraphs
  (2026-09-28): (1) the browser encrypts the entered key to a
  device-generated age recipient, the fleet only forwards the opaque
  block, never the plain key; (2) the browser-side encryption script's
  limit (protects against a leaked database/logs/backups/passive reading,
  not against an actively taken-over fleet server -- the UI shows the
  script's sha256 so the landlord can compare it against the operating
  manual); (3) the agent never writes the live tenant data at all --
  thermoctl/Zigbee2MQTT stay mounted read-only, the agent only stages, a
  separate Go program (**P5.5c**, below) moves staged data into place.
- **Acceptance:** real end-to-end test over the real fleet app and real
  TLS (browser step simulated with `pyrage`, plus a real `node` + the real
  vendored JS interop test); plaintext key rejected; wrong/second device
  cannot fetch; expired block gone; block deleted after one fetch (proven
  race-safe under real concurrent access, not just sequentially); the
  landlord's key string never appears in the fleet's sqlite file, the
  blob-storage directory, or the agent's own tmp tree; agent refuses to
  stage over an already non-empty live store, and refuses a second staging
  while a previous one is still unconsumed; wrong key fails clean, nothing
  staged; a conflicting `age_recipient` at registration time is refused
  (`409`), not silently accepted.
- **Depends on:** P5.5a (this package) -- reuses `protocol.backups`,
  `fleet.backup_storage`, and the same age recipients/identity concept.
- **Read back by:** main session (security principle 3: the landlord's
  decryption key must only ever be passed through, never stored).

### P5.5c -- Restore mover (Go, next to the watchdog)
- [x] done -- see `docs/STATUS.md`'s P5.5c section for the full design.
- **Goal:** the other half of section 15.3's second "Decided afterward"
  paragraph (2026-09-28): "Moving it into the real data directories is
  done by a small, separate Go program on the bare system next to the
  watchdog (same module, no dependency, no network, section 18.4's
  language rule), and only if no operational data exists there yet." P5.5b
  (agent-side) only ever stages a decrypted restore into its own directory
  plus a manifest (`agent/restore.py`, `MANIFEST_FILENAME`,
  `docs/STATUS.md`'s P5.5b section) -- nothing in this scaffold yet reads
  that staging directory back out and moves it into
  `thermoctl_db_path`/`zigbee2mqtt_dir`.
- **What it has to do**, per the manifest P5.5b already writes
  (`{staging_dir}/manifest.json`: `backup_id`, `staged_at`, and a `files`
  list of `{path, size_bytes, sha256}` relative to `staging_dir`):
  1. Notice a staged restore is waiting (the manifest's presence, same
     check `agent/restore.py::_staged_restore_already_pending` already
     uses on the agent side) -- likely watched via the same kind of
     periodic poll the watchdog's own main loop already uses, not a new
     mechanism.
  2. **Re-check the live directories are empty itself, authoritatively**
     (CLAUDE.md security principle 5, and this package's own "the agent's
     own check is advisory only" reasoning) -- never trusts that the
     agent already checked this; a compromised or buggy agent process
     must not be able to route around this by staging anyway.
  3. Validate every file named in the manifest actually exists in
     `staging_dir`, is a **regular file with exactly one hard link**
     (refuses symlinks, FIFOs, device nodes, directories masquerading as
     a listed path, and -- cross-review finding, `docs/STATUS.md` -- a
     multiply-linked file, which would let this program's own chown/chmod
     mutate an inode reachable from somewhere else entirely; the same
     `lstat`-before-`open` discipline `agent/safe_io.py` already applies,
     ported to Go, no dependency), and its size/sha256 match the manifest
     exactly. `manifest.json` itself gets the identical discipline plus a
     size cap (cross-review finding) before it is ever parsed.
  4. Move each validated file into its real destination
     (`thermoctl_db_path`/`zigbee2mqtt_dir`), then remove the staging
     directory (manifest included) so a later restore can stage again.
     **Revised after cross-review (see `docs/STATUS.md`'s "P5.5c
     cross-review fixes" entry):** not a direct `os.Rename` of the staged
     path -- that path stays writable by the untrusted agent process for
     as long as this program runs, so a "validate, then later rename the
     path" split lets it be swapped for a symlink (or a hard link
     elsewhere) in between. Instead: read, hash, *and* copy each
     validated entry's bytes, in one continuous pass from the single
     descriptor it was opened and checked on, into a fresh file this
     program itself creates inside the real destination directory
     (`O_CREAT|O_EXCL|O_NOFOLLOW`), fsynced and fchown/fchmod'd via that
     descriptor (never by path) -- only *then* renamed into its final
     name, always within that same destination directory (so it can
     never fail with `EXDEV`, which also means "staging and destination
     must be on the same filesystem" no longer applies at all). Every
     file is validated and copied before a single one is renamed into
     place.
  5. Write a status file the agent itself reads back to report the final
     result to the fleet (mirrors `agent.loop`'s own LED/health status
     file convention, section 22.3/17) -- the mover has no fleet
     connection of its own (no network, per the module's own constraint)
     and cannot call `POST /v1/restore/result` itself.
- **Constraints, same as the watchdog's own (CLAUDE.md security principle
  6, adopted for this program too):** Go, statically built, `go.mod`
  without a single third-party dependency, **no network access, no
  registry** -- this program only ever touches the local filesystem and
  (for step 5) a local status file the agent polls.
- **Section:** 15.3's second "Decided afterward" paragraph, 17, 18.3/18.4.
- **Depends on:** P5.5b (this package) -- consumes exactly the staging
  layout and manifest format `agent/restore.py` already produces; a change
  to that format is a contract change between the two, not a private
  detail of either.
- **Read back by:** main session (CLAUDE.md security principle 5/6: the
  authoritative empty-check and the no-network/no-dependency constraint).

### P5.5d -- Restore mover open points (resumable finalize, narrower
### ReadWritePaths=, a `_resolve_prospective` false positive)
- [x] done -- see `docs/STATUS.md`'s P5.5d section for the full design.
- **Goal:** close the three open points left after the P5.5c/P5.5b
  cross-review merges: (1) a partial finalize (some files already renamed,
  a later rename failed) left the mover refusing every retry forever
  (`DetailLiveStoreNotEmpty`) even after the underlying problem was fixed
  -- fixed with a small, root-owned pre-rename journal
  (`backup_id`/manifest sha256/final paths+hashes) that authorizes a later
  run to resume, but only if every live file that exists still matches it
  exactly; (2) the mover's systemd unit granted write access to the whole
  of `/var/lib/thermoctl-agent` (holding the agent's own device token and
  age identity) only so it could remove the staging directory *entry* --
  narrowed to clearing only the directory's *contents*, so
  `ReadWritePaths=` now names exactly the staging directory itself; (3)
  `agent.restore._resolve_prospective` produced a false-positive refusal
  for a `..` inside the not-yet-existing suffix of a staging path --
  fixed by folding the missing suffix onto the already-resolved existing
  prefix with `os.path.normpath`, never resolving a `..` that follows an
  existing (possibly symlinked) component lexically.
- **Files:** `watchdog/cmd/thermoctl-restore-mover/{journal.go (new),
  atomicwrite.go (new), main.go, move.go, manifest.go, validate.go,
  status.go, mover.go, thermoctl-restore-mover.service}`,
  `image/common/tmpfiles.d/{thermoctl-agent.conf,
  thermoctl-restore-mover.conf}`, `image/common/agent-compose.yml`,
  `tools/check_image_config.py`, `agent/restore.py`, `agent/__main__.py`,
  `watchdog/check_contract.sh`, `tests/{test_image_config.py,
  test_agent_restore.py}`,
  `watchdog/cmd/thermoctl-restore-mover/{mover_test.go,
  validate_test.go}`.
- **Section:** 15.3's second "Decided afterward" paragraph, 17, 18.3/18.4.
- **Depends on:** P5.5b, P5.5c (this package only closes their own open
  points, no new design surface).
- **Read back by:** main session (CLAUDE.md security principle 5/6: the
  resumability decision, the narrower systemd sandboxing, and the staging
  path safety check are all part of the security boundary this package
  touches).

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

## Step 6 -- Section 12's owner decisions (2026-10-01)

Three packages closing the "Decided afterward" paragraphs the project
owner added to section 12 in the main session: P6.1 (retention + tenant
change), P6.2 (login hardening -- passkeys + encrypted TOTP secrets), P6.3
(fault acknowledgement + per-device battery/signal). Developed in
parallel worktrees; `PROTOCOL_VERSION` and `fleet/migrations/versions/`
are re-chained at merge if more than one bumps/adds at once.

### P6.1 -- Retention and tenant change (section 12) **SR**
- **Goal:** a background job deleting heartbeats older than 90 days and
  faults/alarms/events older than 365 days (both periods configurable),
  never the audit log; and an explicit UI action, per apartment, that
  rotates the apartment's device token (old token stops working
  immediately, the agent recovers it via a signed challenge with its
  existing Ed25519 device key -- no private key ever leaves the device)
  and deletes that apartment's heartbeats, events, faults, alarms, and
  command log excerpts (not backups, not the audit log, not inventory).
- **Files:** `fleet/data_retention.py` (new), `fleet/storage.py`,
  `fleet/auth.py`, `fleet/app.py`, `fleet/ui_routes.py`,
  `fleet/templates/ui/tenant_change_confirm.html` (new),
  `fleet/migrations/versions/0018_retention_and_tenant_change.py` (new),
  `agent/token_rotation.py` (new), `agent/commands_channel.py`,
  `agent/__main__.py`.
- **Section:** 12.
- **Acceptance:** retention boundaries exact (a row exactly at the cutoff
  kept, one microsecond older deleted) with an injected clock; unrelated
  tables (backups, audit log, command log excerpts, diagnostic bundles)
  never touched by the retention job; old token 403 after rotation for an
  unrelated caller, 401 "re-authenticate" for the device that actually held
  it; replay/wrong-key/expired-challenge all refused; deletion scope exact
  (another apartment's history untouched); CSRF/login/mandatory-reason
  enforced on the UI action; agent recovers once, end to end, over a real
  TLS harness, never loops.
- **Depends on:** P4.2b (the signed-challenge design this package reuses),
  P5.5a (the backup-retention pattern this package's own retention job
  mirrors).
- [x] done -- see `docs/STATUS.md`'s own P6.1 section for the full design:
  no new protocol model (the token-rotation challenge/token pair reuses
  P4.2b's `TokenChallenge`/`TokenRequest`/`TokenIssued` under a distinct
  domain-separated message, `thermoctl-fleet/token-rotation/v1`), so
  `PROTOCOL_VERSION` is unchanged. `fleet.auth`'s new 401 "re-authenticate"
  signal (`WWW-Authenticate: Bearer error="reauth_required"`) is how the
  agent learns to run the recovery flow, surfaced to it as
  `agent.commands_channel.CommandStreamReauthRequired`, retried at most
  once by `agent.__main__._run_agent`. Migration `0017` (renumbered to
  `0018` at the main-merge below, after P6.3's own parallel
  `0017_fault_acknowledgements.py`). `ruff`/`mypy`/`pytest` all clean,
  coverage 99% overall, every new line in this package's own files at
  100%.
- [x] **cross-review fix** (2026-10-02, see `docs/STATUS.md`'s own entry
  for the full account): both token-rotation endpoints now also require
  the OLD token as Bearer (`fleet.auth.require_apartment_reauth_old_
  token`) -- closes a found-and-reproduced gap where anyone who merely
  knew a rotated apartment's (non-secret) id could overwrite the real
  device's single active nonce and permanently lock it out. Rotation-
  pending state is now also cleared by `confirm_device`/`issue_device_
  token`/`remove_device`, not only by completing the rotation. A new
  `agent/reauth_backoff.py` persists an exponential backoff across process
  restarts so a persistently failing re-authentication cannot crash-loop a
  supervised process. Two further owner decisions folded in: the 365-day
  retention limit now applies only to cleared/closed alarms (an open one
  survives regardless of age); tenant change additionally deletes the
  apartment's diagnostic bundles (row and blob, scoped to that apartment).

### P6.3 -- Fault acknowledgement + per-device battery/signal **SR**
- [x] done -- see `docs/STATUS.md`'s own P6.3 section for the full design
  and verification output.
- **Goal:** close P3.2's "per-device battery/signal values are not in the
  heartbeat wire protocol" open point and P3.4's "no acknowledge/confirm
  mechanism" open point, both per section 12's 2026-10-01 "Decided
  afterward" paragraphs: an acknowledgement applies to the fault's current
  occurrence only (it shows again if the identical kind/zone fault clears
  and reopens with a new `since`); a per-device heartbeat list of
  `(device_id, battery_percent, signal_quality)` only, `device_id` a
  structurally-enforced opaque Zigbee IEEE address, no name/room/measured
  value ever representable.
- **Files:** `protocol/heartbeat.py` (`PerDeviceState`,
  `DeviceState.per_device`, `MAX_PER_DEVICE_ENTRIES`),
  `protocol/version.py` (`PROTOCOL_VERSION` 8 -> 9), `fleet/storage.py`
  (`FaultAcknowledgementRecord`, `acknowledge_fault`,
  `list_fault_acknowledgements_for_apartment`,
  `list_all_fault_acknowledgement_keys`),
  `fleet/migrations/versions/0017_fault_acknowledgements.py`,
  `fleet/ui_tasks.py` (acknowledged occurrences filtered out of
  "Aufgaben"), `fleet/ui_apartment.py` (`PerDeviceDisplay`,
  `OpenFaultDisplay`'s new acknowledgement fields), `fleet/ui_routes.py`
  (`POST /ui/apartments/{id}/faults/acknowledge`),
  `fleet/templates/ui/apartment.html`.
- **Section:** 9, 12.
- **Acceptance:** an acknowledged occurrence (exact `apartment_id`/
  `fault_kind`/`zone`/`since` match) is hidden from "Aufgaben" and shown as
  quittiert on "Eine Wohnung"; the identical kind/zone fault reopening with
  a new `since` is unaffected by the old acknowledgement and shows again;
  acknowledgement requires login + CSRF, is re-validated against the
  apartment's *currently* open faults (never trusted from the form alone),
  and is audited (who/when/optional note); a heartbeat with a name-like
  `device_id` or an extra field (`name`/`room`/`temperature`) on a
  per-device entry is rejected with 422; the list is bounded; the
  fleet-wide aggregates are unchanged. `fleet/ui_apartment.py::PerDeviceDisplay
  .label` is always `None` in this scaffold -- the P4.1 inventory has no
  table mapping a Zigbee device id to a landlord-chosen label (its
  `Device` table tracks the base station hardware, not individual Zigbee
  devices), left as an open point for a future package, not invented here.
- **Depends on:** P3.2/P3.4a (the views this closes open points in), P1.3
  (storage layer).
- **Read back by:** main session (a new optional heartbeat field is a
  protocol change, CLAUDE.md's "not a data collector"/"a field may only
  ever be added" principle).

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
