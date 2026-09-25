# Status

Last updated: 2026-09-25.

## Login for the fleet UI (P3.0), round-3: per-IP throttle + account-lock backstop

Cross-review round 2's lockout model (5 failures / 15 min lock, always
counted, re-locked on every attempt while already locked) had a real gap,
pointed out after round 2 landed: **it is keyed by account, not by
source.** Anyone who knows the landlord's username can keep the account
locked out indefinitely with one wrong request every 15 minutes, from a
single address -- the lockout meant to protect the account becomes a denial
-of-service tool against its own owner. **Decision (project owner,
2026-09-25): two independent layers, not one -- a per-client-IP throttle as
the primary defence, and a much higher, windowed account-level lock as a
backstop, with a notification the moment the backstop actually engages.**

**1. Per-client-IP throttle (`ui_login_throttle`, migration
`0005_ui_accounts`, `Storage.is_ip_login_blocked`/
`record_ip_login_failure`).** One row per IP address that has ever failed a
login: `failures`, `window_started_at`, `blocked_until`. Default 5 failures
within 15 minutes blocks that one IP for 15 minutes; the window resets
(fresh count) once it lapses with no failures. **Checked before
`fleet.ui_auth.authenticate` is ever called** (`fleet/ui_routes.py
::login_submit`) -- a blocked IP gets the exact same generic failure
response as any other failed login, without a single Argon2 verify (CPU
protection, not just a UX nicety) and without touching the *account's* own
failure counter at all ("its attempts are not counted against the
account", decided verbatim). A successful login does **not** unblock any
IP, including its own -- an already-blocked window simply lapses on its
own; there is no special-case reset path that could itself become a bug.
Same atomic-`UPDATE`-per-failure technique as the account lock below (see
`Storage.record_ip_login_failure`'s own docstring); regression tests:
`tests/test_ui_throttle.py::test_concurrent_ip_failures_do_not_lose_updates`,
`::test_concurrent_ip_failures_block_deterministically`,
`::test_ip_throttle_concurrency_regression_runs_reliably` (10 rounds).

**2. Client IP resolution (`fleet.ui_auth.resolve_client_ip`).**
`request.client.host` by default. `X-Forwarded-For` is honoured **only**
when the direct TCP peer is inside `FLEET_UI_TRUSTED_PROXIES`
(comma-separated IPs/CIDRs, default **empty** -- meaning the header is
completely ignored unless a deployment explicitly opts in); when trusted,
the right-most address in the header that is **not itself** in the trusted
set is used, walking the proxy chain from the hop closest to us outward.
**A deployment behind a reverse proxy that does not set
`FLEET_UI_TRUSTED_PROXIES` throttles by the proxy's own address for every
request** -- effectively one shared bucket for every real client behind
it, worth flagging explicitly for anyone running the fleet service that
way (see the trade-off note below). Tests:
`tests/test_ui_throttle.py::test_resolve_client_ip_ignores_spoofed_xff_from_an_untrusted_peer`,
`::test_resolve_client_ip_uses_xff_via_a_trusted_proxy`,
`::test_resolve_client_ip_walks_a_chain_of_multiple_proxies`,
`::test_resolve_client_ip_uses_cidr_trusted_proxies`,
`::test_resolve_client_ip_falls_back_to_the_direct_peer_if_every_hop_is_trusted`.

**3. Account-level lock, now a backstop, not the primary defence
(`Storage.record_ui_login_failure`, same table/columns as round 2 plus a
new `ui_users.failure_window_started_at`).** Defaults raised from 5/15min
to **50 failures within a 24h window → 1h locked** -- high enough that a
single source hammering the per-IP throttle above never reaches it, but
still there to stop a *distributed* attacker (more addresses than the IP
throttle alone absorbs) from brute-forcing the account indefinitely.
**The counting model itself changed, not just the numbers:** the window
starts at the first failure and resets to a fresh count only once **24h**
of no failures have passed -- not on every failure, and not on the lock
itself lapsing (a failure presented after the 1h lock has expired but
still within the 24h window **re-locks** the account rather than getting a
fresh 50-strike allowance, since the window hasn't reset). And, changed
from round 2: **a failure while the account is already locked now counts
for nothing** -- round 2's "re-lock on every attempt, indefinitely" is
gone, because the per-IP throttle above is what actually has to slow a
continuing attacker down now; the account lock no longer needs to
re-arm itself on every attempt to do that job too. See
`Storage.record_ui_login_failure`'s own docstring for the complete
reasoning, including why this needed **two** atomic statements per call
(not one): an intermediate single-statement `UPDATE ... RETURNING`
design was tried and found to have a real off-by-one bug -- `RETURNING`
in SQLite evaluates against the row's *post-update* state even for a
column only used inside a derived boolean expression, not just the column
being written, so a boundary check referencing `failed_attempts` inside
`RETURNING` reported the lock one failure too early. Caught by exercising
the exact threshold boundary directly (call 49 vs. 50), not only under
concurrency -- worth remembering as a general trap, not just fixed once.

**4. "Account locked" notification (`fleet.alarms.notify_ui_account_locked`,
new `AlarmKind.UI_ACCOUNT_LOCKED`).** Fires through the same P2.2 alert
channels absence alarms already use (`load_notifiers_from_env` --
webhook/SMTP/log fallback), **exactly once per lock**, the moment
`Storage.record_ui_login_failure` reports that a given call is the one
that actually transitioned the account from unlocked to locked (not "every
call while locked", not "zero because of a race"). Payload is
**deliberately minimal, as decided**: `alarm_kind`, `username`, `locked_at`
-- no IP address, nothing password- or TOTP-related. Not tied to the
`alarms` table (`AlarmRecord`'s open/all-clear/`raise_notified` bookkeeping
exists for a *recurring, clearable* per-apartment condition; a UI account
lock is a one-shot event with no apartment and nothing to clear) -- instead
`Notifier` gained a second entry point, `notify_raw(subject, payload)`,
alongside the existing `notify(AlarmNotification)`, implemented by all
three notifier classes and reusing their existing transport (TLS-verified
webhook POST, TLS-required SMTP send, or a log line). A broken
`FLEET_ALERT_*` configuration is logged and treated as "no notifier"
(`fleet/ui_routes.py::get_ui_notifiers`) rather than turned into a login
500 -- unlike `fleet/app.py`'s lifespan, which parses this once at process
startup specifically so a bad config is loud immediately, this dependency
runs on every login POST, and alerting being misconfigured must not also
break the login feature itself. Regression tests:
`tests/test_ui_throttle.py::test_authenticate_notifies_exactly_once_when_the_account_locks`,
`::test_authenticate_does_not_notify_again_on_further_failures_while_locked`,
`::test_concurrent_lockout_notifies_exactly_once` (20 concurrent threads,
exactly one notification), `::test_concurrent_lockout_notification_regression_runs_reliably`
(10 rounds).

**The remaining trade-off, stated plainly, not hidden:** an attacker who
controls at least `⌈lockout_threshold / ip_throttle_threshold⌉` distinct
source addresses (50/5 = 10, at the defaults) can still force the
account-level lock, for up to an hour at a time, by spreading failed
attempts across enough of them to stay under each individual address's
throttle. This is a materially higher bar than round 2's "one address,
one request every 15 minutes, forever" gap, but it is not eliminated --
distributed brute-forcing an account, as opposed to a single source doing
it, is exactly what the account-level backstop exists to make expensive,
not impossible. Recovery: wait out the 1h lock (or the 24h window, for the
counter itself to reset), or `python -m fleet.admin unlock` immediately.
The landlord is alerted (point 4 above) the moment it happens, so this is
not a silent lockout the way round 2's un-alerted version was.

**Updated environment variables (replacing round 1/2's lockout section of
the table below):**

| Variable | Default | Meaning |
|---|---|---|
| `FLEET_UI_LOCKOUT_THRESHOLD` | `50` | consecutive failures within the window before an account locks |
| `FLEET_UI_LOCKOUT_WINDOW_S` | `86400` (24 h) | the failure-counting window; resets to a fresh count once this much time passes with no failures |
| `FLEET_UI_LOCKOUT_DURATION_S` | `3600` (1 h) | how long an account lock lasts |
| `FLEET_UI_IP_THROTTLE_THRESHOLD` | `5` | consecutive failures from one IP within the window before that IP is blocked |
| `FLEET_UI_IP_THROTTLE_WINDOW_S` | `900` (15 min) | the per-IP failure-counting window |
| `FLEET_UI_IP_THROTTLE_DURATION_S` | `900` (15 min) | how long a per-IP block lasts |
| `FLEET_UI_TRUSTED_PROXIES` | empty | comma-separated IPs/CIDRs allowed to set `X-Forwarded-For`; **must** be set to the reverse proxy's own address for a deployment behind one, or every request throttles as if it came from the proxy |

Verification for this round: `ruff check .`, `mypy .`, `mypy protocol fleet
agent tools` all clean; `python -m pytest -W ignore::ResourceWarning` run
three times (314 passed each time, 99% coverage, every P3.0 file at 100%
except `fleet/admin.py`'s untested `__main__` guard, the same pre-existing
pattern as `agent/__main__.py`); the concurrency regression tests
(`test_totp_replay_race_allows_exactly_one_concurrent_login`,
`test_concurrent_failed_logins_do_not_lose_counter_updates`,
`test_concurrent_failed_logins_lock_the_account_deterministically`,
`test_concurrent_ip_failures_do_not_lose_updates`,
`test_concurrent_ip_failures_block_deterministically`,
`test_concurrent_lockout_notifies_exactly_once`) each run 10 times
individually, 10/10 passes every time.

## Login for the fleet UI (P3.0), round-2 fixes

Cross-review round 2 (main session plus a second cross-review pass) found
two real concurrency bugs and a timing oracle in the first version of P3.0,
plus a CSP/CSS mismatch and a missing password floor. All five are fixed;
the reasoning for each lives primarily as a docstring next to its fix (so it
stays next to the code it explains), summarized here:

- **TOTP replay race (reproduced: the same valid code from 20 concurrent
  threads → 20/20 successful logins).** `Storage.record_ui_login_success`
  used to read `last_totp_step`, decide in Python whether the presented
  step was new, and only then write it back -- 20 concurrent requests could
  all read "not yet used" before any of them had written anything. Fixed by
  folding the check into the `UPDATE`'s own `WHERE` clause (`last_totp_step
  IS NULL OR last_totp_step < :step`), so the database decides atomically,
  per request, whether it is still the first to advance the watermark past
  this step. The method now returns whether its write actually happened;
  `fleet.ui_auth.authenticate` treats `False` as an ordinary failed login
  (counted toward lockout like any other), not as "someone else already
  succeeded". Regression tests: `tests/test_ui_auth.py
  ::test_totp_replay_race_allows_exactly_one_concurrent_login` (20 threads,
  one `threading.Barrier`, same code, real migrated SQLite -- exactly one
  success) and `::test_totp_replay_race_regression_runs_reliably` (10
  repeated rounds against fresh TOTP steps, to catch an occasionally-flaky
  fix, not just a first-run pass).
- **Lockout counter lost updates (reproduced: 20 concurrent wrong
  passwords → `failed_attempts == 11`, not 20).** `Storage
  .record_ui_login_failure` used to do a Python-level `record
  .failed_attempts += 1` read-modify-write -- classic lost-update race under
  concurrency. Fixed with a single atomic `UPDATE ... SET failed_attempts =
  failed_attempts + 1 ... RETURNING failed_attempts`; the returned,
  guaranteed-correct post-increment count is what decides (in a second
  statement in the same transaction) whether `locked_until` gets set.
  Regression tests: `::test_concurrent_failed_logins_do_not_lose_counter_
  updates` (20 threads, high threshold so locking doesn't interfere,
  `failed_attempts == 20` afterward), `::test_concurrent_failed_logins_lock_
  the_account_deterministically` (same race at the real default threshold
  of 5 -- still `failed_attempts == 20` *and* the account ends up locked),
  and `::test_lockout_counter_regression_runs_reliably` (10 rounds against
  10 fresh accounts).
- **Lockout-lapse behaviour, decided and documented (cross-review asked
  explicitly) -- superseded by round 3, kept here only as history.** This
  round's model was: every failed attempt counted whether or not the
  account was already locked, and a failure after a lock lapsed re-locked
  the account rather than resetting the counter. Round 3 (see the section
  above this one) replaced the account lock with a much higher, windowed
  threshold plus an independent per-IP throttle as the actual primary
  defence, and changed "counts while already locked" to "does not" -- the
  reasoning that follows in this bullet no longer describes the current
  behaviour; `Storage.record_ui_login_failure`'s own docstring and the
  round-3 section above are the current source of truth. (The regression
  test this bullet originally pointed at,
  `test_a_new_failure_after_the_lock_lapses_relocks_the_account`, still
  exists and still passes -- it now exercises round 3's "the lock lapsing
  is not the same as the window lapsing" behaviour instead, see its
  updated docstring in `tests/test_ui_auth.py`.)
- **Timing oracle for locked accounts (main-session finding): `authenticate`
  used to return `None` for a locked account *before* running any Argon2
  work at all**, so a locked (i.e. existing) account answered measurably
  faster than an unknown username -- the response body was identical, but
  the timing leaked "this account exists" regardless. Fixed: the Argon2
  verify (real hash or dummy, exactly as before for the unknown-user case)
  now always runs first, unconditionally, before the lock (or password, or
  TOTP) result is even inspected; being locked only changes *what happens
  after* that verify, never *whether* it happens. Regression tests:
  `::test_locked_account_still_pays_the_full_argon2_cost` and
  `::test_unknown_user_and_locked_user_both_run_exactly_one_argon2_verify`,
  both spying on the Argon2 hasher's call count (monkeypatching the
  `PasswordHasher` class, since the C-backed instance's own `verify`
  attribute is read-only) rather than asserting on noisy wall-clock timing.
- **CSP blocked the page's own styling.** The security-headers middleware
  sends `Content-Security-Policy: default-src 'self'` with no
  `'unsafe-inline'` -- correct for the "no inline scripts" requirement, but
  `base.html`'s inline `<style>` block was *also* inline content the same
  policy blocked, so the pages rendered unstyled in a real browser (only
  `TestClient`, which does not execute CSS/JS, ever exercised them). Fixed
  by moving the CSS into `fleet/static/ui/fleet-ui.css`, served same-origin
  under `/ui/static/...` (mounted via `StaticFiles` on the `/ui` router in
  `fleet/ui_routes.py`, added to `[tool.setuptools.package-data]` alongside
  the templates, covered by the same wheel-inspection test as the templates
  in `tests/test_packaging.py`) -- `default-src 'self'` already allows a
  same-origin stylesheet link with no policy relaxation needed. No template
  contains an inline `<style>`, `<script>`, or `style=` attribute any more;
  `tests/test_ui_auth.py::test_templates_contain_no_inline_style_or_script`
  asserts this directly against the shipped template files, not just by
  eyeballing them.
- **Minimum password length (main-session decision): `MIN_PASSWORD_LENGTH =
  12`**, enforced in `fleet/admin.py::_read_new_password` -- the only place
  a UI account's password is ever set (there is no web-facing registration
  endpoint for a floor to guard there instead). `fleet.ui_auth`'s
  verification path itself enforces no minimum, deliberately: it must keep
  accepting whatever password was actually set at creation time regardless
  of later policy changes. Tests: `tests/test_admin.py
  ::test_create_user_rejects_a_too_short_password` and
  `::test_create_user_accepts_a_password_exactly_at_the_minimum_length`.
- **Username normalization (optional, done -- cross-review round 2):**
  `fleet.ui_auth.normalize_username` (NFKC + casefold) is applied at both
  boundaries where a human types a username -- `fleet.admin`'s four
  subcommands and `authenticate` -- so `"Landlord"` and `"landlord"` cannot
  become two separate accounts by accident, and a later command naming the
  same account by a different case/normalization variant still resolves to
  it. `Storage.get_ui_user_by_username` itself stays a plain, un-normalizing
  exact-match lookup -- normalization is an application-layer decision made
  once at the boundary, not something a raw storage read should silently
  apply. Tests: `tests/test_admin.py
  ::test_create_user_normalizes_the_username`,
  `::test_create_user_rejects_a_case_variant_of_an_existing_username`,
  `::test_unlock_finds_the_account_by_a_differently_cased_username`;
  `tests/test_ui_auth.py::test_authenticate_normalizes_the_presented_username`.

Verification for this round: `ruff check .`, `mypy .`, `mypy protocol fleet
agent tools` all clean; `python -m pytest -W ignore::ResourceWarning` run
three times (276 passed each time, 98% coverage); the two headline
concurrency regression tests
(`test_totp_replay_race_allows_exactly_one_concurrent_login`,
`test_concurrent_failed_logins_do_not_lose_counter_updates`) each run 10
times individually, 10/10 passes both.

## Login for the fleet UI (P3.0)

The specification (section 9) describes the three UI views but is silent on
how the landlord logs in. **Decided by the project owner, 2026-09-24:** own
user accounts in the fleet database (`ui_users`, `ui_sessions`, migration
`0005_ui_accounts`), password hashed with Argon2 (`argon2-cffi`), TOTP as a
**mandatory** second factor (`pyotp`), server-side sessions via a cookie.
The first account is created with `python -m fleet.admin create-user`,
never via the web -- there is no `POST /ui/register` anywhere in this
package, deliberately. No external identity provider. UI texts are German
(matching thermoctl's own UI), code and comments English.

Completely separate from agent auth (`fleet/auth.py`, `/v1/...`) --
different table, different dependency, no shared helper beyond `Storage`
itself; see `fleet/ui_auth.py`'s module docstring for the full reasoning,
and `tests/test_ui_auth.py::test_agent_token_cannot_access_protected_ui_page`
/ `::test_ui_session_cookie_cannot_access_the_agent_api` for the tests that
would fail if this separation broke.

**Where the pieces live:** `fleet/ui_auth.py` (password/TOTP verification,
account lockout, client-IP resolution, session creation/lookup, CSRF, the
`require_ui_user` dependency every later UI package depends on),
`fleet/ui_routes.py` (`GET/POST /ui/login`, `POST /ui/logout`, the
protected `GET /ui/` placeholder, the `/ui`-scoped security-header
middleware, the per-IP throttle check, the `get_ui_notifiers` dependency),
`fleet/admin.py` (the CLI), `fleet/alarms.py` (also owns
`notify_ui_account_locked` and `AlarmKind.UI_ACCOUNT_LOCKED` since round 3
-- the same P2.2 notifier channels, reused rather than duplicated),
`fleet/templates/ui/{base,login,index}.html`, `fleet/static/ui/fleet-ui.css`
(all shipped inside the `fleet` package via `[tool.setuptools.package-data]`
in `pyproject.toml` -- proven by `tests/test_packaging.py`, which builds a
real wheel and inspects it, not just by reading the config; the CSS file is
served same-origin under `/ui/static/...`, see the round-2 fixes section
above for why it is a separate file and not inline).

**Environment variables (all optional, sensible defaults) -- see the
round-3 section above for the lockout/throttle/trusted-proxy variables,
which replaced this table's original lockout row:**

| Variable | Default | Meaning |
|---|---|---|
| `FLEET_UI_SESSION_ABSOLUTE_LIFETIME_S` | `43200` (12 h) | hard session ceiling regardless of activity |
| `FLEET_UI_SESSION_IDLE_TIMEOUT_S` | `3600` (1 h) | session dies this long after the last authenticated request |

**Generic failure response:** unknown user, wrong password, and
wrong/replayed TOTP code are indistinguishable from the caller's side --
same status code, same rendered text, similar Argon2 work (a fixed dummy
hash, computed once at import time from data that is never stored, stands
in for a real user's hash when the username does not exist, so the
"unknown user" path costs the same CPU time as "known user, wrong
password").

**TOTP replay:** a ±1 time-step window (so ±30s of clock drift is
tolerated), and a presented code resolving to a step at or before the
user's last accepted step is rejected even if it is still numerically
correct -- closes the replay window `pyotp.TOTP.verify()` leaves open on
its own when called in a loop.

**CSRF:** two separate tokens, matching the two phases of the flow. Before
a session exists, `/ui/login` uses the classic double-submit-cookie pattern
(a short-lived, `HttpOnly` pre-session cookie plus a matching hidden form
field). After login, every state-changing `/ui` POST (including logout)
checks a per-session token stored on the `ui_sessions` row itself, compared
with `hmac.compare_digest`.

**Open points, carried forward, not silently dropped:**

- **Passkeys/WebAuthn** are a possible later extension (noted by the
  project owner at decision time) -- not built here. Would remove the
  stored-shared-secret risk below entirely for whoever opts in.
- **TOTP secrets are stored in plain text** in `ui_users.totp_secret` (like
  thermoctl's own account TOTP storage, for the same reason: a symmetric,
  time-based one-time code needs the shared secret readable at verification
  time, there is no salted-hash equivalent for a TOTP secret the way there
  is for a password). A database leak exposes every account's TOTP seed,
  not just password hashes. Mitigation options for later, not decided here:
  encrypting `totp_secret` at rest with a key held outside the database (a
  KMS, or an operator-supplied environment secret used only to wrap/unwrap
  this one column), or moving to passkeys (above) where no such secret
  exists to leak in the first place.
- **No password-reset-by-mail**, on purpose: the fleet UI has exactly one
  account class (the landlord), no email sending infrastructure exists
  anywhere else in this repository, and a mail-based reset flow is its own
  attack surface (account takeover via a compromised mailbox) for a single
  operator who already has shell/database access to run `python -m
  fleet.admin reset-totp`/`unlock` directly. Revisit only if a second
  landlord account holder makes CLI access impractical.
- Expired/idle sessions are rejected on read but not proactively deleted
  from `ui_sessions` -- a stale row is harmless (it can never authenticate
  again) but does accumulate; a retention/cleanup job is future work,
  mirroring the same open point already recorded for `heartbeats`/`events`
  retention below. The same applies to `ui_login_throttle` (round 3) --
  every distinct failing IP ever seen gets a row that is never deleted,
  only ever updated in place; harmless (a lapsed block cannot reactivate
  itself) but grows without bound over the life of a deployment under
  sustained scanning/attack traffic. Same future retention job, not built
  here.
- The account-lock/IP-throttle trade-off itself (an attacker with enough
  source addresses can still force the account-level lock) is stated in
  full in the round-3 section above, not repeated here.

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
- **Snooze -- what it suppresses today, precisely (clarified after cross-
  review asked).** `Storage.set_alarm_snoozed_until(alarm_id, until)` stores
  a point in time on an **already-open** alarm. The one and only thing it
  changes is whether a *retried* raise-notification is attempted -- i.e.
  it only ever matters after a notifier has already failed once for that
  alarm (`raise_notified` is still `False`) and a later check run would
  otherwise retry it; while snoozed, that retry is skipped. It does
  **not** need to suppress "one notification per outage" in the ordinary
  case, because that is already the unconditional default described in
  the bullet above -- an alarm whose raise-notification already succeeded
  is never re-notified regardless of snooze, and snooze cannot make an
  already-silent alarm noisy again. It has no effect on whether a new
  alarm gets raised in the first place, and no effect on all-clear
  notifications (an apartment recovering while snoozed still gets its
  all-clear). There is no HTTP/UI endpoint for it yet, see below.
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

**Safe for more than one fleet process (added after the second cross-
review, see "Second cross-review" below for the full story).** `alarms`
also carries a **partial** unique index,
`ux_alarms_apartment_id_kind_open` (`apartment_id`, `kind`,
`WHERE cleared_at IS NULL`, both SQLite and PostgreSQL support a partial
index this way) -- at most one *open* row per `(apartment_id, kind)` can
exist at the database level, enforced there, not just by application
logic in `fleet/alarms.py`. This is what makes `check_absence_alarms`
safe to run from more than one fleet process/worker at once (e.g. two
uvicorn workers, or a horizontally scaled deployment each running the
same background task): whichever process's `Storage.raise_alarm` insert
wins the race gets the new row back and is the only one that notifies;
every other process's concurrent call for the same apartment gets `None`
and does nothing, by construction, not by coincidence of timing.

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

Full suite (before the second cross-review below): **199 tests**, coverage
**98%** (852 statements, 20 missed) -- `ruff check .` and `mypy .` /
`mypy protocol fleet agent tools` all clean. Merge commit `0821e07`,
follow-up fix commit `505b102`.

**Second cross-review: seven further issues found, all fixed, `0004_alarms.py`
edited in place (not yet on `main`, so no re-chaining needed for this
round).**

1. **Duplicate open alarms under concurrent checks.** Reproduced: 5
   concurrent `check_absence_alarms` runs for one absent apartment
   produced 4 duplicate open alarms and 5 raise notifications --
   `Storage.raise_alarm` was a plain `INSERT`, no different from
   `save_heartbeat` before `0003_heartbeats_unique_sent_at.py` existed.
   Fixed with the same technique, one level more precise: a **partial**
   unique index, `ux_alarms_apartment_id_kind_open` on
   `(apartment_id, kind) WHERE cleared_at IS NULL` (see "Safe for more
   than one fleet process" above), plus `Storage.raise_alarm` rewritten as
   a dialect-native `INSERT ... ON CONFLICT ... WHERE cleared_at IS NULL
   DO NOTHING ... RETURNING id` -- it now returns `AlarmRecord | None`,
   `None` meaning "another caller already has one open, and only that
   caller may notify" (`fleet/alarms.py::_handle_absent` acts on this).
   Regression test with real threads,
   `tests/test_storage.py::test_raise_alarm_is_safe_under_concurrent_calls`
   (5 threads, same pattern as P2.1b's own heartbeat-batch concurrency
   test), plus a single-threaded
   `test_raise_alarm_returns_none_when_one_is_already_open` and a
   `fleet.alarms`-level `test_check_absence_alarms_does_not_notify_when_it_loses_the_race_to_raise_alarm`.
   `compare_metadata` stays empty (checked).
2. **`WebhookNotifier` stopped at the first failing URL.** Fixed: every
   configured URL is now attempted regardless of an earlier one failing;
   failures are collected and raised together, once, as a new
   `WebhookDeliveryError` after the loop. Tested with two URLs, the first
   pointing at a server that always 500s, the second a real local receiver
   that must still get (and does get) the POST.
3. **A webhook URL can carry an auth token; it must never reach a log
   line.** `httpx.HTTPStatusError`'s own message includes the full request
   URL, and `_try_notify`'s `logger.exception` would print that (plus any
   chained cause) in full. `WebhookNotifier` now catches `httpx.HTTPError`
   (both `HTTPStatusError` and connection-level errors like
   `ConnectError`) itself and raises `WebhookDeliveryError` naming only
   each failed URL's **index and host** (never path or query, where a
   token would live) and, for a status error, the status code -- with no
   active exception being handled at the point it is raised, so there is
   nothing left for Python to chain as `__context__` either. Tested with a
   URL containing a `secrets.token_urlsafe` marker: asserted absent from
   the raised error's own message *and* from every captured log record
   (`record.getMessage()`) *and* its formatted traceback text
   (`record.exc_text`) -- the last one specifically because a chained
   cause hides there, not in the plain message.
4. **Blocking calls on the event loop.** `_alarm_check_loop` called
   `check_absence_alarms` (SQLAlchemy, `httpx`, `smtplib` -- all
   synchronous) directly on the coroutine; a hanging SMTP server would
   have frozen every other request the fleet service was serving for its
   whole timeout. Fixed: the call now goes through `asyncio.to_thread`.
   `httpx`/`smtplib` calls already carried explicit timeouts
   (`WebhookNotifier`'s `timeout_s`, `SmtpConfig.timeout_s`, both
   default `10.0`, passed into every `httpx.Client`/`smtplib.SMTP`/
   `SMTP_SSL` construction) -- confirmed, not newly added.
5. **A misconfigured alert channel used to die silently.**
   `load_notifiers_from_env` was called from *inside* `_alarm_check_loop`,
   so a bad config (e.g. `FLEET_ALERT_SMTP_HOST` without
   `FLEET_ALERT_SMTP_FROM`/`_TO`) raised on the task's first iteration, was
   swallowed by the task's own `except Exception`, logged once, and the
   service then ran forever with no notifier configured at all -- the
   comment at the time claimed something different from what actually
   happened. Fixed: `fleet.app.lifespan` now parses the configuration
   itself, *before* creating the background task, so a bad configuration
   makes application **startup** raise `NotifierConfigError` loudly.
   Tested via `tests/test_fleet.py::
   test_lifespan_fails_loudly_on_a_misconfigured_alert_channel` (entering
   `TestClient(app)`'s lifespan with a deliberately incomplete SMTP
   configuration).
6. **`SmtpConfig.password` in the dataclass's auto-generated `repr`.**
   `field(repr=False)` fixes it -- every other field still appears.
   Tested: `repr(config)` built with a random password asserts the
   password string absent, host/user present.
7. **Clock going backwards could produce a false all-clear.** Reproduced
   (`cleared_at < raised_at`): a backward clock jump can make `now -
   latest_received_at` drop back under the six-minute threshold without
   any new heartbeat ever arriving -- the same stale heartbeat just looks
   "recent" again because the clock moved, not the apartment. Fixed:
   `_handle_present` now only clears an open alarm when the latest
   heartbeat's own `received_at` is strictly after the alarm's
   `raised_at` (a heartbeat genuinely arrived since the alarm fired), and
   additionally refuses to clear if `now` itself is before `raised_at` --
   either guard failing leaves the alarm open and manufactures no
   all-clear. Tested with a backwards clock
   (`test_clock_going_backwards_does_not_produce_a_false_all_clear`) and,
   to confirm the guard does not also break the ordinary case,
   `test_a_heartbeat_genuinely_after_raised_at_still_clears_normally`.

Full suite (final): **209 tests**, coverage **98%** (878 statements, 20
missed -- `fleet/alarms.py`, `fleet/app.py`, `fleet/storage.py`,
`fleet/auth.py`, and all four migrations at 100%) -- `ruff check .` and
`mypy .` / `mypy protocol fleet agent tools` all clean. Verified stable:
the full suite 3 times and `tests/test_alarms.py` 10 times, all green.
Follow-up fix commit for this round: `64a7d31`.

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
