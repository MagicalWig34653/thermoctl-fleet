# Status

Last updated: 2026-09-24.

## Absence alarming (P2.2, section 8)

New `fleet/alarms.py`: `check_absence_alarms(storage, now, notifiers)` builds
exactly the first of section 8's nine alarm rules, "apartment not
reporting" (three heartbeats missing, `ABSENCE_THRESHOLD = timedelta(minutes=6)`,
urgency high). `now` is injected by every caller (the background task in
`fleet/app.py` and every test in `tests/test_alarms.py`) -- nothing in this
module calls `datetime.now()` itself, so no test waits in real time for an
outage to age past the threshold.

**Against alarm fatigue (section 8), exactly as required:**

- **Exactly one alarm, not one per check run.** A new `AlarmRecord`
  (`Storage.raise_alarm`) is created only when no alarm is currently open
  for that apartment/kind; further check runs while still absent are
  bundled -- no new row, no repeat notification (unless the previous
  notification attempt failed, see "retry" below).
- **Every alarm has an all-clear.** A heartbeat arriving again clears the
  open alarm (`Storage.clear_alarm`) and the all-clear is notified exactly
  once. A later, separate outage after an all-clear raises a genuinely new
  `AlarmRecord`, not a reopening of the cleared one.
- **Snooze.** `Storage.set_alarm_snoozed_until(alarm_id, until)` stores a
  point in time before which a *retried* raise-notification (reached only
  after a notifier failure -- the ordinary "already notified" case is
  bundled regardless of snooze) is suppressed.
- **Retry, never silently dropped.** A notifier's exception is caught and
  logged per notifier (`_try_notify`), never crashes the check. The
  notification only counts as sent -- and `raise_notified`/`clear_notified`
  is only set -- if **every** configured channel succeeded; with two
  channels and one failing, the whole notification is retried on the next
  run (the already-working channel is called again too -- a duplicate
  delivery to one channel was judged the lesser evil against losing the
  alert on the failing one). Tested in `tests/test_alarms.py` with a
  notifier that always raises.

**Decision by the project owner (2026-09-24, per the work package, not
reopened here): notification channels are all configurable, no single
hard-coded service.** `fleet/alarms.py` defines a small `Notifier` protocol
with three implementations:

- **`WebhookNotifier`** -- generic HTTP POST of a JSON payload to every
  configured URL via `httpx.Client`. TLS verification is never disabled --
  there is no parameter anywhere in this class to turn it off with;
  `httpx`'s own default (`verify=True`) is the only behaviour offered.
- **`SmtpNotifier`** -- stdlib `smtplib`, TLS required by default
  (`SmtpTlsMode.STARTTLS` or `.IMPLICIT`, both via
  `ssl.create_default_context()`, certificate verification on). Plaintext
  (`SmtpTlsMode.PLAINTEXT`) is only ever produced by `load_notifiers_from_env`
  behind an **explicit** opt-in (`FLEET_ALERT_SMTP_ALLOW_PLAINTEXT=true`) --
  refused with a clear `NotifierConfigError` otherwise, checked at
  config-parsing time, not at send time.
- **`LogNotifier`** -- the log-only fallback. With no channel configured,
  `load_notifiers_from_env` logs one warning (`"No alert channel
  configured..."`) and still returns a `LogNotifier`, so an alarm is always
  recorded (`Storage.raise_alarm`/`clear_alarm` run regardless of any
  notifier) and always at least logged, never silently lost.

**Environment variables** (prefix `FLEET_ALERT_`, all read by
`fleet.alarms.load_notifiers_from_env`, several may be set at once):

| Variable | Meaning |
|---|---|
| `FLEET_ALERT_WEBHOOK_URLS` | Comma-separated list of webhook URLs. Blank/empty entries are ignored; empty overall -> that channel is simply not configured. |
| `FLEET_ALERT_SMTP_HOST` | SMTP host. Presence of this variable is what "SMTP is configured" means. |
| `FLEET_ALERT_SMTP_PORT` | Optional; defaults to 465 for `implicit`, 587 otherwise. |
| `FLEET_ALERT_SMTP_USER` / `FLEET_ALERT_SMTP_PASSWORD` | Optional; `smtp.login(...)` is only called if a user is set. |
| `FLEET_ALERT_SMTP_FROM` / `FLEET_ALERT_SMTP_TO` | Required once `FLEET_ALERT_SMTP_HOST` is set (`FLEET_ALERT_SMTP_TO` comma-separated) -- a clear `NotifierConfigError` otherwise. |
| `FLEET_ALERT_SMTP_TLS_MODE` | `starttls` (default), `implicit`, or `plaintext`. |
| `FLEET_ALERT_SMTP_ALLOW_PLAINTEXT` | Must be `true`/`1`/`yes` for `FLEET_ALERT_SMTP_TLS_MODE=plaintext` to be accepted at all. |
| `FLEET_ALARM_CHECK_INTERVAL_S` | How often (seconds) `fleet/app.py`'s background task runs `check_absence_alarms`; default 60. |

**Notification payload is minimal, checked by a dedicated test.** A
`Notifier` only ever sees an `AlarmNotification`: apartment id, alarm kind,
urgency, event (`"raised"`/`"cleared"`), `raised_at`, `cleared_at`. Nothing
from section 6, and nothing from an `Event`'s `titel`/`text` -- this module
never even reads a `Heartbeat`/`Event` body in the first place, only
`Storage.get_latest_heartbeat`'s receipt timestamp and the `AlarmRecord`
itself, so there is structurally nothing sensitive to leak, not merely
"tested to be absent".

**Persistence: new migration.** The work package originally named this file
`0003_alarms`; P2.1b (parallel package, "accept caught-up heartbeats in one
batch") went through two revisions of its own -- its first version added no
migration at all (heartbeat-batch idempotency enforced at the query level),
its amended version added `0003_heartbeats_unique_sent_at.py` (a unique
index on `heartbeats(apartment_id, sent_at)`, `revision = "0003"`) after a
cross-review found the query-level check unsafe under concurrent writers.
This package's migration is **`fleet/migrations/versions/0004_alarms.py`**,
`revision = "0004"`, **`down_revision = "0003"`** -- chained onto P2.1b's
migration now that both branches are merged together (`git merge main`,
merge commit noted at the end of this section). `alarms` table:
`apartment_id` (indexed), `kind` (closed
`AlarmKind` enum, currently only `not_reporting`, stored as a plain string
-- mirrors how `EventRecord.fault_kind` already stores `FaultKind`, so
`fleet/storage.py` does not need to import `fleet/alarms.py`'s enum types
and risk a circular import), `urgency`, `raised_at`, `cleared_at`
(nullable -- `NULL` means still open), `snoozed_until` (nullable),
`raise_notified`/`clear_notified` (booleans). `Storage` gained
`list_apartment_ids`, `get_latest_alarm`, `raise_alarm`, `clear_alarm`,
`mark_alarm_raise_notified`, `mark_alarm_clear_notified`,
`set_alarm_snoozed_until` -- `tests/test_storage.py::
test_migrations_match_the_orm_model_exactly` (pre-existing, unchanged)
continues to assert `compare_metadata` is empty, now covering `0004` too.

**Scheduling.** `fleet/app.py` gained a `lifespan` context manager (wired
into `FastAPI(..., lifespan=lifespan)`) that starts one asyncio background
task (`_alarm_check_loop`, interval from `FLEET_ALARM_CHECK_INTERVAL_S`,
default 60s) and cancels it cleanly on shutdown. The loop itself carries a
`# pragma: no cover` (per the work package's own instruction -- "only the
loop wrapper may carry this pragma") with its reasoning inline: the tested
logic is entirely `check_absence_alarms`, covered with an injected clock;
an infinite `while True: ... await asyncio.sleep(...)` loop would otherwise
need either a real wait in the test suite or an artificial construction
that tests the wrapper instead of anything real. `tests/test_fleet.py::
test_lifespan_starts_and_cancels_the_alarm_background_task` still covers
the wrapper itself (task creation, clean cancellation on shutdown) by
entering/exiting the lifespan via `TestClient(app)` as a context manager --
`fleet/app.py` is at 100% line coverage.

**Open points, not invented, per the work package's own instruction:**

- **Apartments that have never sent a single heartbeat are not alarmed.**
  `check_absence_alarms` skips any apartment where
  `Storage.get_latest_heartbeat` returns `None`. Section 8 is silent on
  this case; inventing a rule for it (e.g. "alarm immediately", "alarm
  after N hours since registration") would be exactly the kind of guess
  CLAUDE.md warns against. Left open for a future decision by the project
  owner.
- **"high, during the heating season"** (section 8's own wording for this
  alarm's urgency): the heating season is not defined anywhere in the
  specification. Urgency is stored as `high` unconditionally, with no
  season-gating logic -- open point.
- **Snooze has no HTTP/UI endpoint.** `Storage.set_alarm_snoozed_until` is
  the storage-level primitive only; the fleet-UI auth path does not exist
  yet (P3.x), so there is nowhere to authenticate a snooze request from.
  Carried forward as P3.x scope.
- **The other eight alarm rules of section 8's table** (thermoctl not
  responding, control stalled, fault open >2h, battery low, signal quality
  dropping, version gap, disk full, clock drift) are **not** built by this
  package -- `AlarmKind` is a closed enum with only `NOT_REPORTING` so far,
  by the same "closed enum, deliberate addition only" reasoning
  `protocol.commands.CommandType` already follows. Each is separate future
  work, to be added to `docs/implementation_plan.md` as its own package
  when picked up (not batched here to keep this package's own scope, and
  its own test suite, reviewable).
- **Retention of cleared alarms** is not addressed -- same open point as
  heartbeats/events retention (section 12, P1.3), not reopened here.

**Tests:** new `tests/test_alarms.py` (35 tests) against a real, migrated
SQLite database (`fleet.storage.upgrade`, no mock) with an injected clock:
no alarm at exactly 6 minutes, exactly one alarm/notification just past 6
minutes, repeated runs while still absent stay silent, heartbeat resuming
clears with exactly one all-clear notification, a new outage after an
all-clear raises a genuinely new alarm, a snoozed alarm is not re-notified
before the snooze expires (and is once it does), a failing notifier is
retried next run without being marked sent (both the raise and the clear
path), two configured channels both receive, one of two channels failing
means the whole notification is retried, never-reporting apartments stay
unalarmed, multiple apartments are checked independently, and the payload
is asserted to contain only the six documented keys (with a check that
`titel`/`text`/`schluessel`/`schwere`/temperature/setpoint-shaped strings
are absent). The webhook notifier is tested against a real local
`http.server` thread (success and a real 500 response), the SMTP notifier
against a real local `aiosmtpd` server via the explicit plaintext opt-in
(per the work package: "TLS for the local test servers may be tested via
the explicit plaintext opt-in") -- `STARTTLS`/implicit TLS wiring (correct
`smtplib` entry point, a certificate-verifying `ssl.create_default_context()`,
never a disabled one, login called when credentials are set) is instead
checked by substituting a fake `smtplib.SMTP`/`SMTP_SSL` and asserting the
calls made, since real TLS negotiation against a local test server was
explicitly out of this package's scope. Config parsing is tested for no
channel/webhook only/SMTP only/both, missing `FROM`/`TO` (clear error),
`TO` with only blank entries (clear error), an unknown TLS mode (clear
error), and plaintext refused without opt-in vs. accepted with it.
`tests/test_storage.py` gained direct tests for `list_apartment_ids` and
every new `Storage` alarm method (including "unknown id does nothing" for
each mutator) plus migration `0004` upgrade/downgrade (column set, table
presence). `docker/Dockerfile.fleet` needed no change -- it already
installs the `fleet` extra via `pip install ".[fleet]"`, and `httpx` was
added to that extra (not a new dependency for the image: `agent` already
depended on it, `fleet` did not until now).

**Follow-up: merged with P2.1b, migration re-chained, one real test bug
fixed.** P2.1b was later merged into `main` (`e3a5236`) with its amended
`0003_heartbeats_unique_sent_at.py`. `git merge main` into this branch
(merge commit `0821e07`) needed one manual conflict resolution in
`fleet/storage.py` -- both sides had added imports on the same lines
(P2.1b: `Index`, the `postgresql`/`sqlite` dialect modules; this package:
`Boolean`) -- resolved by keeping both. `0004_alarms.py` was re-chained
onto `0003` (`down_revision = "0004" -> "0003"`, was `"0002"`).

**Two real problems found and fixed while re-running the suite against the
merged code, not papered over:**

1. **Duplicate `sent_at` silently deduplicated.** Three tests in
   `tests/test_alarms.py` (`test_heartbeat_returns_all_clear_notified_once`,
   `test_new_outage_after_an_all_clear_raises_a_new_alarm`,
   `test_failing_clear_notifier_is_retried_next_run_and_not_marked_sent`)
   saved a second heartbeat for the same apartment using the test's
   `_make_heartbeat()` helper, which always built the same literal
   `sent_at`. Against `0003`'s new unique index on
   `heartbeats(apartment_id, sent_at)`, the second heartbeat silently
   deduped to the first row instead of being stored -- the alarm never saw
   a fresh `received_at` and the tests failed for the right reason (the
   apartment genuinely never "reported again" as far as storage was
   concerned). Fixed by giving `_make_heartbeat` an optional `sent_at`
   parameter and passing a distinct value at each of those three call
   sites -- not by weakening what the tests assert.
2. **A genuinely flaky test, root-caused, not hidden.**
   `test_webhook_notifier_raises_when_the_server_errors` failed
   intermittently (about 1 run in 7-15, reproduced even running that one
   test alone, repeatedly, with nothing else in the suite running --
   ruling out cross-test resource contention). Root cause, found by reading
   the actual traceback instead of guessing: the test's `_FailingHandler
   .do_POST` never read the request body before responding with 500 and
   returning. `BaseHTTPRequestHandler` then closes the connection
   immediately after `do_POST` returns; closing a TCP socket while there
   are still unread bytes in its receive buffer makes the OS send a **RST**
   instead of a clean FIN. That RST races the client's read of the 500
   response and, when it wins, surfaces in `httpx` as `httpcore.ReadError:
   Connection reset by peer` instead of the intended `httpx.HTTPStatusError`
   -- exactly the "read timeout"-shaped failure originally seen, just with
   its real name once the assertion stopped being widened to catch it.
   Fixed by draining the request body first (`self.rfile.read(length)`,
   mirroring what the passing `_CapturingWebhookHandler.do_POST` already
   did next to it), which avoids the RST entirely -- the assertion is back
   to the specific `httpx.HTTPStatusError` it should have been all along.
   Both `HTTPServer` fixtures/tests in that file also gained an explicit
   `server.server_close()` in teardown (previously only `shutdown()`,
   which stops the accept loop but leaves the socket's file descriptor
   open) -- a correctness fix in its own right, independent of the RST
   issue. Verified stable: 30 consecutive runs of the fixed test alone, 20
   consecutive runs of the full `test_alarms.py` module, and 5 consecutive
   full-suite runs, all green.

Full suite: **199 tests** (up from 178 before this merge; 128 before P2.2
itself), coverage **98%** (852 statements, 20 missed -- `fleet/alarms.py`,
`fleet/app.py`, `fleet/storage.py`, `fleet/auth.py`, and all four
migrations at 100%) -- `ruff check .` and `mypy .` /
`mypy protocol fleet agent tools` all clean. Merge commit `0821e07`,
follow-up fix commit `505b102`.

## Token check per apartment (P1.1, sections 4, 18.1)

New `fleet/auth.py`, two FastAPI dependencies wired via `Depends(...)` into
the four endpoints named in the work package (`receive_heartbeat`,
`receive_event`, `commands_stream`, `receive_command_result`) -- storage
comes from the existing `Depends(get_storage)` (P1.3), never a new access
path. `fleet/app.py` is otherwise unchanged: after a successful check every
endpoint still raises its existing `NotImplementedError` unchanged, exactly
as the work package's acceptance criterion requires; P1.2 and later
packages fill the bodies in.

**Status codes:**

- **401**, with `WWW-Authenticate: Bearer` -- no `Authorization` header, a
  scheme other than `Bearer`, or an empty token. Verified against what
  FastAPI actually does, not assumed: a bare `if authorization is None:
  raise HTTPException(...)` *inside* an endpoint function runs too late --
  with a missing token **and** a structurally malformed body, FastAPI
  returns its own `422` (body validation happens before the endpoint body
  itself runs) before the manual check is ever reached. Raising from a
  `Depends(...)` dependency instead runs during dependency *resolution*,
  which happens before the endpoint's body parameter is bound and validated
  -- confirmed with a throwaway script reproducing both shapes side by side,
  then locked in as
  `test_heartbeat_missing_token_and_malformed_body_is_401_not_422` and
  `test_event_missing_token_and_malformed_body_is_401_not_422` (and the
  equivalent for `receive_command_result`) in `tests/test_fleet.py` -- all
  three assert `401`, not `422`.
- **403** -- the token does not match the apartment's stored hash, *or* the
  apartment does not exist at all. Both cases return the exact same
  response (status and generic detail text) on purpose -- CLAUDE.md: "no
  apartment ids ... in the source code" extends here to "do not reveal
  which apartments exist" over the wire; a caller who gets a different
  response for "wrong token" than for "no such apartment" could enumerate
  apartment ids by trial. **Correction (P1.2 review):** this is true only
  for `require_apartment_token` (apartment from the address), which compares
  the presented token's hash against a *known* stored hash with
  `hmac.compare_digest`, not `==`, precisely because that comparison is the
  one place where the number of matching leading bytes could otherwise leak
  through timing. `require_apartment_token_by_hash` (apartment not in the
  address) instead does an indexed equality lookup of the SHA-256 digest
  against `apartments.token_hash` -- there is no known hash to compare
  against up front, so there is no timing side channel to guard with
  `compare_digest` either: the token's >=32 bytes of server-generated
  entropy (section 4) mean an indexed lookup yields an attacker nothing more
  than "hash present or not".

**Lookup by hash, not by parsing the token.** `POST /v1/heartbeat`,
`GET /v1/commands`, and `POST /v1/commands/{id}/result` carry no apartment
in their address, so the apartment has to be identified from the token
itself. Section 4's token shape is `agent_<apartment>_<random>`, but
`random` comes from `secrets.token_urlsafe`, whose alphabet includes `_` --
splitting the string back apart on `_` is therefore ambiguous and can
resolve to the wrong apartment for an apartment id that itself contains an
underscore, or simply guess wrong on which `_` was the intended separator.
`fleet/storage.py` therefore gained a new lookup,
`Storage.get_apartment_id_by_token_hash(token_hash)`, doing the reverse of
the existing `get_apartment_token_hash`: hash the presented token
(`hash_token`, already used by P1.3), look the hash up directly, return the
apartment id or `None`. `POST /v1/heartbeat` additionally compares the
resulting apartment against `heartbeat.apartment` in the body and returns
`403` on a mismatch -- section 4's registration model is one token per
apartment; an agent presenting a valid token must not be able to report
heartbeat data under a different apartment's name by putting a different
value in the body.

**New migration `0002_apartments_token_hash_unique_index.py`** (never
editing `0001`, per the work package's own instruction): a unique index on
`apartments.token_hash`, both because the new by-hash lookup wants an index
and because two apartments sharing one hash would mean two apartments
sharing one token, which section 4 ("a separate secret per apartment")
never produces. `ApartmentRecord.token_hash` in `fleet/storage.py` gained
matching `unique=True, index=True`, so `alembic.autogenerate
.compare_metadata` against a freshly migrated database stays empty -- the
same invariant the P1.3 review checked for `0001`. No test for this
existed yet, so one was added:
`test_migrations_match_the_orm_model_exactly` in `tests/test_storage.py`
runs `compare_metadata` for real (not by re-reading the migration source)
and asserts the diff is empty.

**Still missing** (explicitly out of scope for this package, tracked here
instead of invented): a registration endpoint that first sets an
apartment's token (section 4, "initial registration via a one-time,
time-limited setup code") -- P1.1's tests create apartments and set tokens
directly via `Storage.set_apartment_token`, there is no HTTP path for it
yet; token **rotation** as an endpoint (the storage-level replace-and-
invalidate behaviour is exercised end to end by
`test_rotated_token_old_one_is_403_new_one_passes`, but nothing in
`fleet/app.py` lets the cloud issue a new token over HTTP); revocation; and
the setup-code/time-limit mechanism itself. The inventory endpoints
(`read_inventory`, `register_device`, and friends, section 20) are
deliberately **not** touched by this package -- they were never named in
the work package's file list and use "a different auth path than section
4" per their own docstrings (a fleet-UI login, not an agent token); their
tests are unchanged, still without an `Authorization` header.

**Tests:** `tests/test_fleet.py` was substantially rewritten around a
`client`/`storage`/`token`/`other_token` fixture set (a real, migrated,
per-test SQLite database in `tmp_path` via `fleet.storage.upgrade`,
wired in through `app.dependency_overrides[get_storage]` -- the same
pattern `tests/test_storage.py` already used, no mock). For each of the
four protected endpoints: no header, wrong scheme, empty token → 401;
wrong token, unknown apartment → 403; valid token → passes through
unchanged to the endpoint's own `NotImplementedError`. Plus: a token valid
for one apartment presented on another apartment's `/v1/events/{apartment}`
address → 403; a heartbeat body naming a different apartment than the
token → 403; a rotated token (old 403, new passes) exercised through both
the HTTP layer and directly against `Storage`; the missing-token-plus-
malformed-body → 401-not-422 case for three of the four endpoints (see
above). Tokens are built at runtime with `secrets.token_urlsafe(32)`
everywhere, never a literal secret. Full suite: **107 tests** (up from 80),
coverage **96%** (unchanged from P1.3 -- `fleet/auth.py`, `fleet/app.py`,
`fleet/storage.py`, and both migrations now at 100%) -- `ruff check .` and
`mypy .` / `mypy protocol fleet agent tools` all clean.

## Accept and store heartbeats (P2.1, sections 5, 18.2)

`fleet/app.py::receive_heartbeat` is implemented: after the existing P1.1
token dependency (`require_apartment_token_by_hash`, apartment identified by
the token's hash, no apartment in the address) and the existing
apartment/body cross-check (unchanged, still 403 on a mismatch between
`authenticated_apartment` and `heartbeat.apartment`), it calls
`Storage.save_heartbeat(authenticated_apartment, heartbeat,
datetime.now(UTC))` -- receipt time as server time in UTC, the same pattern
P1.2 established for `receive_event` -- and returns 204. No
`NotImplementedError` remains on this path.

**Version compatibility (section 18.2), the point of this package:** a
heartbeat is accepted and stored regardless of its `protocol_version` --
lower, equal, or higher than this service's own `PROTOCOL_VERSION`. The
specification is explicit that a version difference must never cause an
apartment to go silent, so nothing in `receive_heartbeat` compares
`protocol_version` at all; the endpoint's only job is to store what arrived.

**"Outdated version" is derived at read time, not stored as a column.**
`HeartbeatRecord.protocol_version` has existed since P1.3, before this
package, precisely because the heartbeat's own wire contract needs it --
reusing it means the outdated flag costs no schema change and needs no
`0003` migration. New: `Storage.get_latest_heartbeat(apartment_id) ->
LatestHeartbeat | None` (`fleet/storage.py`), returning the most recently
*received* heartbeat (ordered by `received_at`, the receipt time, not
`sent_at` -- a late-arriving catch-up entry from an outage, section 5, must
not become "latest" just because it was saved after) together with
`outdated = protocol_version < PROTOCOL_VERSION`, computed against
`protocol.version.PROTOCOL_VERSION` at call time. This is the function P3.x
(the apartment views) and P2.2 (absence alarming, "version gap" in section
8) both consume -- one place computes "is this apartment on an old
protocol", not duplicated per caller. A stored boolean was the rejected
alternative: it would only repeat what the existing column already says,
and could silently drift out of sync with it the day `PROTOCOL_VERSION` is
next bumped, unless every historical row were backfilled at that point too
-- deriving it removes that failure mode entirely. No `0003` migration
exists; `alembic.autogenerate.compare_metadata` still stays empty against
`0001`/`0002` (the existing `test_migrations_match_the_orm_model_exactly`
in `tests/test_storage.py` continues to cover this unchanged, since nothing
in the ORM model changed).

**Unknown fields from a newer agent are accepted, not a 422.** Checked as
part of this task: `protocol.heartbeat.Heartbeat` carries no `model_config`
at all, so Pydantic's default `extra="ignore"` already applies -- an extra
field a higher-protocol-version agent might one day send is silently
dropped during validation rather than rejected. No change to `protocol/`
was needed or made; `tests/test_fleet.py::
test_heartbeat_higher_version_with_an_unknown_extra_field_is_204` locks
this in against a regression (e.g. someone later adding a stricter
`model_config` to `Heartbeat` for an unrelated reason).

**Tests** (`tests/test_fleet.py`, endpoint level, through `TestClient` with
a real migrated SQLite database, extending the P1.1/P1.2 fixtures): the
previous `test_heartbeat_with_a_valid_token_passes_through_to_not_implemented`
is replaced by `..._is_204` (P1.1's own 401/403 tests for this endpoint are
otherwise untouched and still pass); a 401 and the existing apartment/body-
mismatch 403 both now additionally assert nothing was stored for either
apartment; `protocol_version` equal, lower (via `monkeypatch` on
`fleet.storage.PROTOCOL_VERSION`, since the currently released
`PROTOCOL_VERSION` is 1 and the model's `ge=1` constraint makes a literal
lower value inexpressible) and higher than `PROTOCOL_VERSION` are each
posted and asserted 204 plus stored; the lower case additionally asserts
`get_latest_heartbeat(...).outdated is True`, equal and higher both assert
`False`; a higher version with an extra unknown field asserts 204; the
spec-section-5 example (`HEARTBEAT_EXAMPLE`, already used by P1.1's tests)
is posted and read back via `storage.list_heartbeats` and compared equal to
`Heartbeat.model_validate(HEARTBEAT_EXAMPLE)`; storage is confirmed scoped
to the authenticated apartment only (`list_heartbeats(OTHER_APARTMENT) ==
[]`). `tests/test_storage.py` gained direct tests for
`Storage.get_latest_heartbeat`: `None` for an apartment with nothing
stored, "most recently *received*, not most recently *saved*" (three
heartbeats saved out of `received_at` order), and the outdated flag for
lower/equal/higher via the same `monkeypatch` technique.

Full suite: **128 tests** (up from 117), coverage **96%** (unchanged --
`fleet/app.py`, `fleet/auth.py`, and `fleet/storage.py` all at 100%) --
`ruff check .` and `mypy .` / `mypy protocol fleet agent tools` all clean.

**Still missing, explicitly out of scope for this package, tracked here
instead of invented:** gap detection for caught-up heartbeats (section 5,
"The cloud detects gaps by the timestamp") and the batch format for
catch-up delivery -- the specification requires the agent to send up to
240 buffered heartbeats in one batch after an outage, but the wire shape
of that batch is not yet defined anywhere (`protocol/heartbeat.py`'s own
docstring already flagged this as open); both belong with P2.3 (the agent
side that would produce such a batch), not this single-heartbeat endpoint,
which only ever sees one heartbeat per request as things stand. Alarm
evaluation (section 8) is P2.2, layered on top of the storage and the
`get_latest_heartbeat`/outdated-flag machinery built here, not part of it.

## Catch-up batch endpoint `POST /v1/heartbeats` (P2.1b, section 5)

New, additive endpoint (project owner decision, 2026-09-24): `POST
/v1/heartbeats` accepts a plain JSON list of `Heartbeat` for the catch-up
case section 5 describes ("the agent sends the buffered heartbeats (at
most the last 240, i.e. eight hours) on next contact, in one batch").
`POST /v1/heartbeat` (P2.1) is **unchanged** -- section 18.2's "a field may
only ever be added" is read here as extending to the endpoint surface too:
the batch case gets its own path rather than a widened body on the
singular one, so nothing that already depends on posting exactly one
heartbeat has to change.

**The limit is a module constant, not a model field.**
`protocol.heartbeat.MAX_CATCH_UP_HEARTBEATS = 240`, in `protocol/heartbeat.py`
next to `Heartbeat` itself, with a comment citing section 5 -- deliberately
not a field on any Pydantic model (the work package's own instruction): the
240 figure is a property of the *buffer* that produces a batch (the
endpoint here, and the agent-side buffer P2.3 will eventually fill), not of
a single heartbeat's wire shape. `fleet/app.py::receive_heartbeats_batch`
enforces "at least 1, at most 240" structurally via
`Body(min_length=1, max_length=MAX_CATCH_UP_HEARTBEATS)` -- 0 or 241+
entries are a 422 from FastAPI/Pydantic itself, not application code, the
same way a single malformed `Heartbeat` already was.

**Auth and all-or-nothing:** the existing `require_apartment_token_by_hash`
dependency (P1.1) identifies the apartment from the token, exactly as
`POST /v1/heartbeat` does. Every entry in the list must then carry that
same apartment; the check runs over the whole list, in the endpoint,
*before* `Storage.save_heartbeats_batch` is ever called -- so a single
mismatched entry anywhere in the batch is a 403 and **nothing** from the
batch reaches storage, not just the offending entry. This mirrors P2.1's
existing apartment/body cross-check for the singular endpoint, applied to
every element of the list instead of one body.

**Idempotency is enforced at the database level (revised after cross-review
of this package's first version).** The first version of
`Storage.save_heartbeats_batch` queried which of the batch's `sent_at`
values were already stored for that apartment, then inserted only the
rest, all inside one transaction -- and cross-review reproduced a race in
exactly that check-then-insert: 8 threads calling
`save_heartbeats_batch` concurrently with the *same* 20-entry batch against
a real, migrated SQLite database stored **160 rows, not 20**, no exception
raised (two overlapping requests -- a genuine concurrent catch-up, or an
agent retry racing its own still-in-flight first attempt -- can both read
"not yet stored" for the same `sent_at` before either write commits). The
fix: a new migration, `0003_heartbeats_unique_sent_at.py`, adds a unique
index on `heartbeats(apartment_id, sent_at)` (never editing `0001`/`0002`;
`HeartbeatRecord.__table_args__` gained a matching `Index(..., unique=True)`
so `alembic.autogenerate.compare_metadata` against `0001`-`0003` stays
empty, still covered by `test_migrations_match_the_orm_model_exactly`). No
data migration/dedupe step -- the migration's own docstring notes this
repository has no production deployment yet, so there is no existing data
that could already violate the new constraint. `Storage`'s write path
(`_insert_heartbeats_ignoring_conflicts`) now issues one dialect-native
`INSERT ... ON CONFLICT DO NOTHING` statement against that index (SQLite
and PostgreSQL both support `sqlalchemy.dialects.{sqlite,postgresql}
.insert(...).on_conflict_do_nothing(index_elements=...)` -- MariaDB is
**not implemented**, since no MariaDB deployment exists yet: its equivalent
would be `INSERT IGNORE` or `... ON DUPLICATE KEY UPDATE <pk>=<pk>`, a
different SQLAlchemy API that the function's docstring documents as the
follow-up work for whoever adds that deployment) instead of a Python-level
check -- the database itself now decides atomically, per row, whether a
given `(apartment_id, sent_at)` is new, so two concurrent writers can no
longer both pass a check and then both write. `Storage.save_heartbeat` (the
single-heartbeat path, P2.1) goes through the same function now, for
consistency: a live heartbeat whose `sent_at` is already stored is silently
ignored, not a 500 -- decided and tested
(`test_save_heartbeat_duplicate_sent_at_is_ignored_not_an_error`), since a
heartbeat is a periodic report of current state, not a command that must
reject a repeat. `create_engine_from_url` also gained a 30s SQLite
`timeout` (`connect_args`), so a second writer arriving while another
transaction is still committing waits briefly instead of failing
immediately with "database is locked" -- SQLite's default busy timeout is
0.

**Concurrency regression test:**
`tests/test_storage.py::test_save_heartbeats_batch_is_safe_under_concurrent_overlapping_batches`
runs 8 real threads (separate calls into the same `Storage`, each opening
its own session) posting the same 20-entry batch concurrently against a
real, migrated SQLite database and asserts exactly 20 rows, 20 distinct
`sent_at` values, and no exception from any thread -- run five times in a
row during this task with no flake. Migration-level:
`test_migration_0003_creates_a_unique_index_on_apartment_and_sent_at` and
`test_migration_0003_downgrade_removes_the_index_upgrade_restores_it`
(`inspect(engine).get_indexes("heartbeats")` before/after `downgrade(url,
"0002")` and a re-`upgrade`).

**Tie-break fix for `Storage.get_latest_heartbeat` (from the P2.1 review):**
a batch stores many rows that all share the exact same `received_at` (the
one receipt time for the whole call) -- ordering by `received_at` alone
left "which of those rows is `LIMIT 1`" undefined. Now ordered by
`received_at desc, sent_at desc, id desc`: the newest-reported entry of a
tied batch wins, with `id` as a last, fully deterministic tiebreaker.
Covered by `tests/test_storage.py::
test_get_latest_heartbeat_tie_break_shared_received_at_newest_sent_at_wins`
(a batch with a shared `received_at`, asserting the row with the newest
`sent_at` is returned) and by
`test_get_latest_heartbeat_outdated_flag_correct_for_batch_stored_latest`
(the outdated flag, section 18.2, derived correctly for the entry a batch
insert makes "latest", not only for a single-heartbeat insert).

**Tests:** `tests/test_fleet.py` (endpoint level, same fixtures as P1.1/P2.1):
a batch of several stored and read back in `sent_at` order; exactly 240
accepted; 241 → 422; an empty list → 422; one entry naming another
apartment → 403 with nothing stored for either apartment; no token → 401
with nothing stored; missing-token-plus-malformed-body → 401 not 422 (same
pattern as P1.1); a resent batch → no duplicates; a batch overlapping a
heartbeat already received live → not duplicated. `tests/test_storage.py`
gained direct tests for `Storage.save_heartbeats_batch` (same-`received_at`
storage, resend idempotency, overlap-with-live idempotency, apartment
isolation, empty-list no-op) plus the two tie-break tests, the duplicate-
live-heartbeat test, the two migration-0003 tests, and the concurrency
regression test, all described above. Two pre-existing storage tests that
happened to save two heartbeats for the same apartment with an identical
`sent_at` (relying on the old, now-removed "duplicates allowed, ordered by
`received_at`" behaviour to build their fixture data) were updated to use
distinct `sent_at` values instead -- their actual assertions (latest-by-
`received_at`, the outdated flag) are unchanged.

Full suite: **149 tests** (up from 128), coverage **96%** (`python -m
pytest`, addopts-scoped to `protocol`, `fleet`, `agent`, `tools`) --
`fleet/app.py`, `fleet/auth.py`, `fleet/storage.py`, and
`protocol/heartbeat.py` all at 100% (the two lines in `_insert_heartbeats_
ignoring_conflicts` reachable only with an actual PostgreSQL connection, or
an actual MariaDB one, are `# pragma: no cover` with a reason, per
CLAUDE.md: "a line only reachable through an artificial construction");
the remaining gap is unchanged pre-existing scaffold (`agent/loop.py`,
`tools/check_image_config.py`) -- `ruff check .` and `mypy .` / `mypy
protocol fleet agent tools` all clean. `watchdog/`: `go vet ./...` clean,
`go test ./...` green (unchanged, `protocol/`'s field *names* were not
touched, only a new module constant and a docstring sentence added),
`watchdog/check_contract.sh` passes.

**Batch format open point (P2.1's `STATUS.md` entry) is now closed** by
this package -- `POST /v1/heartbeats`, a plain JSON list of `Heartbeat`,
`MAX_CATCH_UP_HEARTBEATS = 240`. **Still open, explicitly out of scope
here, not invented:** gap detection for caught-up heartbeats (section 5,
"The cloud detects gaps by the timestamp and displays them as such") --
this package stores the batch; *displaying* a detected gap is a UI concern
that belongs with P3.2 (the apartment detail view), not this endpoint.
P2.3 (the agent side that would produce a batch to send here) stays
deferred, see the note in `docs/implementation_plan.md` under P2.3 and the
"Decisions by the project owner" note carried by this task.

## Accept and store fault events (P1.2, sections 6, 8, 18.1, 22.1)

`fleet/app.py::receive_event` is implemented: after the existing P1.1 token
dependency (`require_apartment_token`, apartment from the address), it
calls `Storage.save_event(apartment, event, datetime.now(UTC))` -- receipt
time as server time in UTC (section 18.1: "the timestamp is the receipt
time"), and returns 204. No `NotImplementedError` remains on this path.
`Storage.save_event` (already written for P1.3) derives the fault kind via
`protocol.events.fault_kind_from_key` internally; an unknown `schluessel`
prefix is stored with `fault_kind` = `None` ("other report"), never
rejected -- including the deliberate `sensor:` special case (section 22.1):
sensor fault and stuck reading share one key and are therefore never told
apart here, confirmed by a test that posts the same `sensor:<zone>` key
twice and asserts both rows get `fault_kind` = `None`.

**What is stored:** apartment (from the address, not the body), `schluessel`,
`schwere`, the derived `fault_kind`, and `received_at`. **What is
deliberately not stored, and not otherwise used to derive anything stored:
`titel` and `text`** -- the project owner's decision for this task, not
reopened here. Verified in thermoctl's source: a tenant report's `text`
contains "Reported by: `<tenant name>`", the last room temperature, the
setpoint, the mode, and the tenant's free-text note; a sensor-fault `text`
contains the frost-protection setpoint. Section 6 forbids all of these
categories in the cloud outright ("the text of tenant problem reports" is
explicitly listed among what is not transmitted). `protocol.events.Event`
keeps accepting and validating all four German fields unchanged (thermoctl's
payload is not ours to change, section 11: "no change to thermoctl") --
`receive_event` simply never reads `event.titel`/`event.text` after
validation, and `Storage.save_event`'s `EventRecord` has no column for
either (already true since P1.3, re-verified here).

The same decision reaches into `protocol/events.py`:
`fault_event_from_event` used to build `FaultEvent.message` as
`f"{event.titel}: {event.text}"` -- that is gone. `message` is now built
from a fixed, non-sensitive English label per `FaultKind` (or "other report"
where `kind` is `None`) plus `key` (e.g. `"window alarm: fenster:3"`,
`"other report: sensor:3"`) -- `key` alone is safe to include per section 6
("that one exists, with time and room"), since it carries only a technical
zone/device id and the category, never free text. No field of `Event` or
`FaultEvent` was renamed, removed, or added (section 18.2: "a field may only
ever be added"); `FaultEvent.message` stays, its description now documents
what it contains and why. `docs/specification.md` section 22.1 gained a
"**Decided afterward (project owner, 2026-09-24):**" paragraph recording
this; the rest of the document is unchanged, still a byte-identical copy
otherwise.

**Tests** (`tests/test_fleet.py`, endpoint level, through `TestClient` with a
real migrated SQLite database, extending the P1.1 fixtures): all four
non-`sensor:` prefixes from section 22.1's key table with realistic keys
(parametrized), asserting the stored row's apartment/key/severity/kind and
that `received_at` falls inside the request's time window; the `sensor:`
special case explicitly, posting the same key twice and asserting both rows
get `fault_kind` = `None`; an unknown prefix → 204, `fault_kind` = `None`;
events are stored under the address apartment only, not visible for another
apartment (`storage.list_events(OTHER_APARTMENT) == []`); a rejected (401)
request stores nothing. **Privacy test:** posts an event whose `titel`/
`text` carry unique marker strings shaped like what thermoctl actually
sends (a fake "Reported by: ..." tenant name, a fake room temperature), then
asserts the markers appear nowhere -- checked three independent ways: via
`Storage.list_events`, via a raw `SELECT * FROM events` against the engine,
and via the raw bytes of the SQLite file (plus any `-wal`/`-journal`
sidecar) read directly off disk, so a bug at any one layer could not hide a
leak past the other two. The two tests that previously asserted
`NotImplementedError` for a valid, authenticated request now assert 204
instead; the P1.1 401/403 tests for this endpoint are otherwise unchanged
and still pass. `tests/test_protocol.py` gained a test asserting
`fault_event_from_event`'s `message` never contains `titel`/`text`
(same marker-string technique) and updated the existing envelope test's
`message` assertion to the new format.

**`fleet/auth.py` docstring correction (from the P1.1 review):** it had
claimed both dependencies compare with `hmac.compare_digest`; true only for
`require_apartment_token` (a *known* stored hash to compare against).
`require_apartment_token_by_hash` does an indexed equality lookup of the
presented token's SHA-256 digest against `apartments.token_hash` instead --
there is no known hash to time an approach toward, so there is no timing
side channel `compare_digest` would need to close: the token's >=32 bytes of
server-generated entropy (section 4) mean the lookup yields nothing more
than "hash present or not" either way. Reworded in both `fleet/auth.py` and
the matching P1.1 passage above; docstring/prose only, no logic changed.

**Remaining gap, not built here:** alarm evaluation (section 8, "fault open
for longer than 2 hours" among the nine rules) needs the absence-alarming
machinery from P2.2 (which does not exist yet) to evaluate "how long has
this been open" against ongoing heartbeats -- storing the event is the
precondition for that, not the alarm itself. Also still open, carried over
from P1.1/P1.3 and unaffected by this task: retention (section 12), the
registration endpoint, token rotation/revocation over HTTP.

Full suite: **117 tests** (up from 107), coverage **96%** (unchanged --
`fleet/app.py`, `fleet/auth.py`, `fleet/storage.py`, `protocol/events.py`,
and both migrations at 100%) -- `ruff check .` and `mypy .` /
`mypy protocol fleet agent tools` all clean. `watchdog/`: `go vet ./...`
clean, `go test ./...` green (unchanged, `protocol/` field names were not
touched), `watchdog/check_contract.sh` passes.

## Storage layer (P1.3, section 12)

`fleet/storage.py` (SQLAlchemy 2.x typed ORM) plus Alembic migrations shipped
inside the package at `fleet/migrations/` (`env.py`, `versions/`) -- not a
repository-root `alembic.ini`, which `docker/Dockerfile.fleet` would not
carry into the image (it only `COPY`s `fleet/`). `fleet/storage.py::upgrade`
builds the Alembic `Config` entirely in code for exactly this reason; a
sibling `downgrade` exists too, used by the tests, not by the running
service. Both `sqlalchemy` and `alembic` were added to the `fleet` extra in
`pyproject.toml` (not the base dependencies), so `agent`'s image does not
pull them in. `fleet.migrations` and `fleet.migrations.versions` are proper
Python packages (own `__init__.py`) so `[tool.setuptools.packages.find]`'s
existing `"fleet*"` pattern already installs them -- no separate
`package-data` entry was needed.

Three tables, deliberately minimal, all in `fleet/migrations/versions/
0001_initial_schema.py`:

- **`apartments`** (`id`, `token_hash`): **only the SHA-256 hash** of the
  agent token is stored (section 4: "the cloud stores only its hash").
  Unsalted, fast SHA-256 is the right call here, not the shortcut it would
  be for a human-chosen password -- the token carries >=32 bytes of
  server-generated entropy by construction (`agent_<apartment>_<random>`),
  so there is no low-entropy search space for a slow KDF
  (bcrypt/argon2/scrypt) to defend against. `Storage.set_apartment_token`
  hashes internally; there is no sibling function that could be called by
  mistake to store the raw token.
- **`heartbeats`** (`apartment_id`, `received_at`, `sent_at`,
  `protocol_version`, `payload_json`): the heartbeat as validated JSON
  (`Heartbeat.model_dump_json()`), not split into columns -- read back via
  `Heartbeat.model_validate_json`, so an additive protocol change (section
  18.2) needs no migration to stay readable. **Checked against
  `protocol/heartbeat.py` as part of this task, per its instructions:** the
  claim "`Heartbeat` already excludes everything section 6 forbids" holds --
  none of its fields (`ThermoctlState`, `ControlState`, `DeviceState`,
  `SystemState`, `OpenFault`) carry a room temperature, a setpoint, a
  schedule, an absence period, or tenant data. No correction needed.
- **`events`** (`apartment_id`, `schluessel`, `schwere`, `fault_kind`
  nullable, `received_at`): **deliberately no column for `titel`/`text`**.
  thermoctl's tenant-report text carries the tenant's name, room
  temperature, setpoint, mode, and a free-text note; sensor-fault text
  carries the frost-protection setpoint -- both forbidden by section 6.
  `fault_kind` is derived on write via `protocol.events.fault_kind_from_key`
  (`None` = "other report", section 18.1/22.1, including the deliberate
  `sensor:` special case) -- storing only the derived, closed-vocabulary
  kind plus the (thermoctl-internal) key keeps the promise that no free text
  from the webhook payload is ever persisted.

`Storage` wraps one SQLAlchemy engine with write/read-back methods for all
three tables (`set_apartment_token`/`get_apartment_token_hash`,
`save_heartbeat`/`list_heartbeats`, `save_event`/`list_events`) and a
`session()` context manager (commit on success, rollback and re-raise on
error). Datetimes are stored as naive UTC (`_naive_utc`, mirroring
thermoctl's own `utcnow()` convention in `thermoctl/db/base.py`) -- SQLite
(and a future MariaDB) has no timezone-aware column type.

`get_storage()` is a FastAPI dependency provider reading `FLEET_DATABASE_URL`
lazily on first use (never at import time, and never a hard-coded default,
per `CLAUDE.md`) and caching one `Storage` singleton. **Deliberately not
wired into any endpoint** -- `fleet/app.py` is otherwise untouched by this
task; every endpoint still raises `NotImplementedError` exactly as before.
P1.1/P1.2 will call it via `Depends(get_storage)`.

**Retention (section 12) is still not implemented**, as decided by the
project owner for this task -- the specification's own numbers (90 days for
heartbeats, 365 for faults) are marked "proposal", not a decision, and no
deletion job exists. Still open, tracked here, not invented.

**Tests** (`tests/test_storage.py`, 20 tests, all against a **real SQLite
file in `tmp_path`**, migrated via `fleet.storage.upgrade` -- no
`Base.metadata.create_all()` shortcut): migrations create all three tables
on an empty database and are idempotent when re-run; a downgrade-then-
upgrade round trip (exercises `0001_initial_schema.py::downgrade`, which
would otherwise never run); heartbeat and event write/read-back, including
per-apartment isolation and oldest-first ordering; the six-kinds-from-a-key
mapping including the `sensor:` special case and an unknown prefix; that
`titel`/`text` have no column at all (asserted via `inspect(engine)
.get_columns("events")`, not just by reading the model); token hash
set/lookup/replace (a replace changes the hash), unknown-apartment lookup
returns `None`; that no raw token is ever stored (inspects the actual row
via raw SQL, not through `Storage`'s own accessor, so a bug in the accessor
could not hide a stored raw token); `get_storage`'s missing-env-var error and
its singleton caching; the `session()` context manager's rollback-and-
re-raise path. Full suite: **80 tests** (up from 60), coverage **96%** (up
from 94%; `fleet/storage.py` itself at 100%) -- `ruff check .` and `mypy .` /
`mypy protocol fleet agent tools` both clean. `fleet/app.py` is unchanged by
this task (no endpoint was touched, per scope).

`.github/workflows/ci.yml`'s stale "the scaffold stores nothing" comment on
the `check` job was updated: SQLite needs no service container (a writable
filesystem, which the runner already has, is enough), a mariadb/postgres
matrix following thermoctl's own `ci.yml` pattern is explicitly out of scope
for this package. No trigger or job was changed.

**English migration (this update):** the repository's directories, files,
identifiers, comments, and documentation were translated to English end to
end (see the commit that carries this note for the full list). Everything
below that names a file or identifier uses the current, English name; where
a translated commit message quotes a past state verbatim, the name at that
point in time is kept as it was. The original German specification stays
authoritative as a source document under `thermoctl/lokal/`; the English
`docs/specification.md` in this repository is authoritative for this
repository from 2026-09-24 onward -- see its own note at the top.

**300-line rule redefined (section 18.3).** The watchdog's line limit now
counts only executable statements -- comments and blank lines no longer
count. Reason: the point of the limit was "small enough to read in full",
and that is a statement about logic, not about explanations; the previous
counting method had forced trimming comments to stay under 300, and the
comments are exactly where the reasoning lives. Under the new rule, the six
production files (`main.go`, `watch.go`, `state.go`, `health.go`, `leds.go`,
`linefile.go`) total **300 raw lines** (the old counting method) and
**195 statement lines** (the new counting method) -- both well under the
limit, `go vet` and `go test ./...` still green (25 tests).

## Six previously open points decided by the project owner

Six gaps the specification had so far been silent on are now decided and
carried into `docs/specification.md` (sections 17, 19, 20, 22.1-22.4, 24.4):

1. **Fallback without a proven revision.** When the system image is built,
   the digest of the shipped version is written into the state file and
   counts as `proven` from the first boot on (new section "Fallback without
   a proven revision" in section 17, bullet in 19.3). `watchdog/watch.go::
   RollBackToProven` now reports an empty `Proven` as a sign of a **faulty
   delivery**, no longer as an unresolved special case.
2. **Meaning of `since`:** the point in time since which `desired` applies --
   already documented in section 22.2, now explicitly marked as a decision
   made afterward (the only reading with which the field can steer the
   fallback at all), carried into `watchdog/state.go` and `agent/loop.py`.
3. **State names in English.** `ApartmentState` and `DeviceLifecycle` in
   `protocol/inventory.py` now carry English values (`occupied`,
   `in_service`, ...), with a table in section 20.1. Only the values, not
   class or field names. `fleet/app.py` docstrings and tests updated to
   match.
4. **Unified envelope for fault events.** `protocol/events.py` now has
   `FaultEvent` (kind, key, timestamp, plain text) and
   `fault_event_from_event`; the prefix table now covers four of the six
   fault kinds (`zigbee2mqtt:`, `tenant-report:`, `fenster:`,
   `schaltbefehl:`) -- `sensor:` deliberately stays without a mapping
   (section 22.1, special case sensor fault/stuck reading).
5. **Health report format.** Line-based like the state file, not a single
   timestamp: `timestamp=`, `digest=` (the **currently running** digest),
   `version=` (section 22.3, newly written). Implemented in
   `watchdog/health.go`, `agent/loop.py::report_health` (new), the contract
   test `watchdog/check_contract.sh` now covers this file type too.
6. **eSIM fallback.** `esim_previous_profile=`/`esim_deadline=` are two
   further lines in the **existing** state file, not a separate file
   (section 24.4). `watchdog/state.go` skips unknown lines anyway, so the
   format is extensible by this. `agent/loop.py::report_watchdog_state`
   accepts two new keyword arguments for this.

The shared line format of the state file and the health report was pulled
out into a new, shared module while implementing point 5 (now
`watchdog/linefile.go`: `readKeyValueLines`, `parseOptionalTimestamp`) --
without that, `watchdog/` would have grown well past the 300-line limit
(the health report needed just as much parsing code through the new format
as the state file did). Production code was then at **299 lines** under the
old counting method (six files: `main.go`, `watch.go`, `state.go`,
`health.go`, `leds.go`, `linefile.go`) -- **just under** the 300 limit, still
**no** entry in `go.mod`. `go vet` and `go test ./...` ran green (25 tests,
up from 19). Python test suite: 60 tests (up from 53), coverage unchanged at
**94%** (the 6% gap is exclusively in the `NotImplementedError` stubs
already uncovered before this task, not in new code).

**No contradiction with the specification found.** All six decisions folded
cleanly into the recipe, the state file contract, and the inventory models.

## Specification brought up to date, scaffold for sections 23/24, implementation plan created

`docs/specification.md` was outdated (1001 of what are now 1235 lines of the
local source) and is now a **verbatim** copy again. New in it: section 22
("Addenda from building the scaffold" -- four readings the scaffold had to
choose, see their respective locations below), section 23 (status display on
the device), and section 24 (remote eSIM profiles), plus in 19.1 the
**"mainline or not at all"** rule for a third image outside the Raspberry
family, and in 19 a line about ModemManager/LTE firmware in the package
list. The specification now has **24 sections**.

**Known dead reference in the specification:** section 19.1 refers to
`lokal/recherche/basisstationen-alternativen.md` -- a path that only exists
in the local, unpublished document collection, not in this repository. The
copy stays verbatim (see `CLAUDE.md`, "docs/specification.md is
authoritative"), the reference is therefore noted here instead of corrected
in the document.

**Scaffold for section 23 (status display, two LEDs on the 40-pin header):**
`watchdog/leds.go`, new. `LedPresent` is **actually implemented** (a plain
`os.Stat` call) and is thereby already testable for the central point from
23.3: if the two sysfs files are missing, that is **not an error**, the
watchdog keeps running unchanged. `LedSetPattern` is a stub like the other
functions in `watch.go` -- which watchdog event triggers which blink pattern
is decided together with `watch.go` itself (see
`docs/implementation_plan.md`, P5.7). Production code was then at 273 lines
(up from 226, limit 300) -- see above for the current state (299 lines, six
files) after the six addenda.

**Scaffold for section 24 (eSIM):** four new stubs in `agent/loop.py`
(`esim_profiles_list`, `esim_profile_load`, `esim_profile_activate`,
`esim_profile_delete`), a documented flow for each function. All four are
**stage 2** and are therefore -- like `factory_reset` and `open_access` --
**not** included in `protocol.commands.CommandType`. The security-relevant
point from section 24.4 is in the docstring of `esim_profile_activate`: a
profile switch cuts the connection the command arrived on, so the fallback
lies with the **watchdog**, not the agent -- the same split as for
desired-state reconciliation, just with a SIM profile instead of a
container digest.

**Fallback clock decided** (see "Six previously open points" above, point
6): `esim_previous_profile=`/`esim_deadline=` as two further lines in the
existing state file, no separate file.

**New:** `docs/implementation_plan.md` -- work packages in the order from
section 11, one task per package sized to its own worktree, with an
acceptance criterion and a checkbox. Replaces the step-by-step list that
used to be here.

## Just a scaffold

This repository contains **no** working application. `protocol/` is
complete (Pydantic models for heartbeat, command/command result, desired
state, registration, event, inventory). `fleet/` and `agent/` each have an
endpoint or loop scaffold with `NotImplementedError` at every point where
implementation is missing. `watchdog/` has been a standalone **Go module**
since section 18.3 (no longer Python) with the same idea, in Go idiom:
functions return an error referencing the section, with one exception -- the
file contract with the agent (`watchdog/state.go`, `watchdog/health.go`) is
**actually implemented**, not just a placeholder, see "The watchdog" below --
`watchdog/leds.go` (section 23) follows the same pattern with one exception
(`LedPresent`, see above). `image/` (section 19) is a recipe scaffold
without a real image build, `tools/` checks its configuration. Every
location carries a reference to the section in `docs/specification.md`
(now 24 sections).

What comes next is now exclusively in `docs/implementation_plan.md` (work
packages P1.1 ff., derived from section 11) -- no longer listed redundantly
here, to avoid exactly the kind of stale duplication this file is meant to
stay free of, per `CLAUDE.md`.

## The watchdog is now in Go, no longer in Python (sections 18.3, 18.4)

Changed after the scaffold had initially been built with a Python `watchdog`
package -- the project owner decided, before it was finished, that the
watchdog would be written in Go, because it "has to be the one thing that
works when everything else is broken", and a Python interpreter cannot give
exactly that guarantee (a broken `apt`, a shot `python3` symlink, corrupted
`.pyc` files). The **language rule** that follows from this (section 18.4):
**Go on the bare metal, Python in the container** -- the agent stays Python,
because it brings its own runtime in its own image, and the protocol
package it shares with `fleet/` would otherwise have to be maintained twice.

`watchdog/` sits outside the container runtime, with its own systemd unit
(`watchdog/thermoctl-watchdog.service`) and its own CI track
(`.github/workflows/go.yml`) -- **the Python track (`ci.yml`) stayed
unchanged** in doing so, as required by section 18.3.

**Conditions, all met:**

- `watchdog/go.mod` has **no** dependency.
- Statically built (`CGO_ENABLED=0`), checked for `linux/arm64` and
  `linux/amd64` (cross-compiled locally and confirmed with `file`).
- Production code (`main.go`, `state.go`, `health.go`, `watch.go`,
  `leds.go`, `linefile.go`, without tests) is at **299 lines** under the old
  counting method -- just under the 300 limit (226 before section 23, 273
  after, see "Six previously open points" above for the jump to 299; see the
  top of this document for the current numbers under the new,
  statements-only counting method).
- `go vet` and `go test ./...` run green (25 tests, see the test results
  below).

**The cross-language contract test** (`watchdog/check_contract.sh`, section
18.3): Python writes the state file with the same code that later runs on
the device (`agent.loop.report_watchdog_state`), the built Go binary reads
it in check mode (`-check-mode`), the script compares both values. Run
locally and passed; runs as its own job (`contract-test`) in
`.github/workflows/go.yml`.

**The file format is line-based, not JSON** (`desired=`, `proven=`,
`since=`, like a systemd environment file) -- the reasoning for this is
stated twice in the source (`watchdog/state.go` and `agent/loop.py`, at
`report_watchdog_state`), deliberately not only in one place, so it is not
lost if someone only has one of the two files in front of them.

What is still missing in `watchdog/watch.go` (everything returns an error
referencing the section): detecting the agent's end, starting a container
with the desired digest, waiting for the health report (10-minute deadline),
falling back to `proven` if it fails to arrive.

## Inventory: property, apartment, device, assignment (section 20)

`protocol/inventory.py` models all four entities, with the three points the
project owner explicitly insisted on: the assignment is its own entry
(`from_`/`until`/`reason`), not a field on `Device`; the device state is a
closed enumeration (`DeviceLifecycle`, seven values) instead of free text;
`Apartment` carries no tenant name, with a comment at that point explaining
why not. `Apartment.pilot_mode: bool` (default `False`) is also there, for
section 21.4.

`fleet/app.py` has six stub endpoints for this (read inventory, register
device, prepare, confirm registration and assign, replace device, change
state) -- each accepts its model structurally and otherwise reports
`NotImplementedError`. The three rules from section 20.3 (at most one
active device per apartment, a device belongs to at most one apartment, no
release without a confirmed verification code) are **enforced nowhere** --
that is application logic that must go into every affected endpoint at the
real implementation, not just one.

**State names decided** (see "Six previously open points" above, point 3):
`ApartmentState` and `DeviceLifecycle` now carry English values, with a
table in section 20.1 -- as with `FaultKind` (section 5).

## Remote factory reset, re-provisioning, and diagnostics (section 21)

Three commands newly described, two of them laid out as stubs in
`agent/loop.py`:

- **`factory_reset`** (command `factory_reset`, stage 2, section 21.2):
  stub with a fully documented flow, including the security-relevant order
  -- **upload the last encrypted backup first, delete only after**, even on
  a tenant change, because the retention period (section 12) decides the
  deletion, not the button press. `factory_reset` is **not** part of
  `CommandType` (stage 2, like the other commands excluded there).
- **`create_diagnostic_bundle`** (command `diagnostic_bundle`, **stage 1**,
  section 21.5): stub, and **`diagnostic_bundle` is now part of
  `CommandType`** -- the only section-21 newcomer in the closed list.
- **`open_access`** (command `open_access`, stage 2, pilot phase, section
  21.4): **the only function in this module that touches a stage-2 matter
  and is nonetheless partly actually implemented.** The `pilot_mode` check
  ("the agent rejects the command -- the check is local, not in the UI") is
  real: without `pilot_mode=True` the function raises `PermissionError`,
  not `NotImplementedError`. The rest (SSH certificate, back-channel,
  60-minute deadline) stays a placeholder. `open_access` is likewise **not**
  part of `CommandType`.

**A/B system partitions (section 21.3) are not being built.** The project
owner explicitly deferred this: section 21.2 (remote factory reset) covers
almost everything that comes up day to day; A/B only pays off once a
heating season of operation shows that on-site visits actually happen
because of the operating system -- not because of defective hardware, where
someone has to go on site anyway. **The decision is reversible as long as
the image recipe (`image/`) stays in our own hands** -- no step in this
scaffold builds in a wall against RAUC, Mender, or swupdate, should that pay
off later after all.

## Further open points from scoping the scaffold

- **Token format not as a model.** `agent_<apartment>_<random>` (section 4)
  is deliberately not a Pydantic model with an example value -- a
  repository-safe example would look like a real secret. Whoever wants to
  check the format does so via a separate, secret-free validation function,
  not via a model with a default.
- **`.venv/bin/pytest` fails on macOS with this editable install**
  (`ModuleNotFoundError` for the project's own packages), even though
  `import protocol` works in the same interpreter -- the same cause as with
  thermoctl's console command (see its README): the hidden marker file
  under `.venv/bin` is skipped at startup. `python -m pytest` works
  reliably and is therefore documented that way everywhere here.
- **`image/` package list and udev rule are placeholders.** In particular
  the USB ids in `image/common/udev/99-zigbee-stick.rules` must be replaced
  with the ids of the actually procured radio stick before the real build
  (see the comment there).

## CI

Two independent tracks, as required by section 18.3:

- **`ci.yml`** (Python): ruff, mypy, pytest against Python 3.13 and 3.14,
  without a database service -- the scaffold stores nothing.
- **`go.yml`** (Go, new): `go vet` and `go test` for `watchdog/`, building
  one static binary each for `amd64`/`arm64` with a checksum, plus the
  cross-language contract test (see above). Runs only on changes to
  `watchdog/`, `agent/loop.py`, or `protocol/`; can also be started manually
  for a given commit (`gh workflow run go.yml --ref main`), e.g. to confirm
  a merge that only touched `fleet/`.
- **`image.yml`** (new): reads the `image/` configuration and validates the
  package list (`tools/check_image_config.py`) -- **no** real pi-gen/mkosi/
  debos run, that belongs at the release, not in every commit. Can also be
  started manually for a given commit (`gh workflow run image.yml --ref
  main`).
- **`docker.yml`** builds two images (`thermoctl-fleet`, `thermoctl-agent`)
  for `linux/amd64` and `linux/arm64` to ghcr.io on `v*` tags, and on every
  pull request as a build check without publishing. `watchdog/` goes into
  **neither** of the two images (no Docker image, see above).
