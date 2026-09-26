# Status

Last updated: 2026-09-26.

## `PROTOCOL_VERSION` bump to 2 for the P4.2b registration models (main
session)

**Decision by the project owner (2026-09-26):** section 18.2's "a number
that increases with every change to the models" is read **literally** --
every change to the models, including purely additive ones, bumps it. The
four models P4.2b added (`RegistrationAccepted`, `TokenChallenge`,
`TokenRequest`, `TokenIssued`) therefore bump `PROTOCOL_VERSION` from 1 to
**2**, reversing the "not bumped" call made at the time (still recorded,
now marked superseded, in this file's P4.2b section below and in
`protocol/registration.py`'s module docstring). The compatibility rules of
18.2 are unchanged: the fleet still accepts an older version and flags it
"outdated" rather than rejecting it, the agent still rejects only commands
of a newer version, and a field is still only ever added.

**Effect:** agents reporting `protocol_version` 1 now show as "outdated
version" on "Das Haus" and "Eine Wohnung", and get an update task on
"Aufgaben" -- expected, not a regression; no agent is deployed yet, so
nothing currently reporting is affected. `docs/specification.md` 18.2 gets
a short "Decided afterward" paragraph stating this; nothing else in the
spec changes. Tests that hard-coded a literal `1` as the *current* protocol
version's default (`tests/test_ui_apartment.py`, `tests/test_ui_tasks.py`,
`tests/test_ui_house.py`, each a `_make_heartbeat` helper's
`protocol_version` default) were changed to default to the
`protocol.version.PROTOCOL_VERSION` constant instead -- those tests do not
care about the outdated flag and would otherwise have started asserting
"outdated" apartments as "fine" ones by accident. Tests that hard-code `1`
to deliberately represent an *older* agent (e.g.
`tests/test_ui_apartment.py::test_outdated_protocol_version_is_flagged`,
several inline heartbeat JSON bodies in HTTP-level tests unrelated to the
outdated flag) were left as literal `1`, now a real older version rather
than a merely hypothetical one. `tests/test_protocol.py
::test_heartbeat_with_lower_protocol_version_is_accepted`'s docstring
("`PROTOCOL_VERSION` is currently 1 -- there is no real older version yet
to test against") was rewritten to use the now-real older version 1
instead of modelling a hypothetical future one.

## Cross-review fixes: P5.6 registry path, resumability, manifest digests

Five findings from P5.6's own cross-review (R1-R5) plus one further,
deeper finding from the main session's read-back (the state file's digest
is a *registry manifest* digest, not a local image ID) -- all fixed in the
same worktree, one commit, before P5.6 merges.

**R1/R5 -- `docker run` without `--pull=never` could reach a registry, and
had no restart policy at all.** Both traced back to the same root cause:
`Start` ran the digest directly (`docker run -d --name thermoctl-agent
<digest>`), a command with no volumes, no environment, and -- the sharper
problem -- no restart policy, so Docker never counted a restart and
"the container restarted three times in a row" (section 17 step 5) could
never actually trigger. **Decided (main session): the agent's real run
configuration lives in a fixed compose file shipped with the system
image**, `image/common/agent-compose.yml` -- `pull_policy: never` (R1) and
`restart: on-failure` (so `RestartCount` means something, and Docker does
not fight the watchdog by resurrecting a container the agent stopped
*itself* to swap). `watchdog/runtime.go`'s `cliRuntime.Start` now does
exactly two things, no more: tag the digest locally (no network), then
`docker compose -f <fixed path> up -d --pull never --force-recreate agent`
(`--pull never` again, belt and suspenders with the file's own
`pull_policy`). The compose file is never sent by the cloud -- section 13's
"no arbitrary compose files" is about what the cloud may hand the agent,
not about this one fixed file baked into the image like
`thermoctl-watchdog.service`. New flag `-runtime-compose` (default
`/etc/thermoctl-agent/compose.yml`); the container name is no longer a
flag at all (`agentContainerName` constant, fixed by the compose file's
own `container_name` -- two places to keep in sync by hand was worse than
one fixed name). `tools/check_image_config.py` gained
`check_agent_compose_file` (plausibility only, no YAML parser pulled in:
checks the handful of substrings that would silently defeat R1/R5 if
lost).

**R4 -- a digest was never validated before reaching argv.** A value
starting with `-` would have been parsed as a command-line option, not an
image reference. Fixed with `digestPattern` (`^sha256:[0-9a-f]{64}$`),
checked in `Start` before anything is even tagged.

**R2 -- resumability: a watchdog restart mid-swap could never roll back an
unhealthy revision.** `Reconcile` used to treat "the agent is running" as
"nothing to do", full stop -- if the watchdog itself died and restarted
while a new, still-unproven digest was running, it would see that digest
running and return OK forever, never checking whether it had actually
proved itself within the deadline. Fixed by anchoring `AwaitHealthReport`'s
deadline to the state file's own `since` (`time.Unix(s.Since,
0).Add(10*time.Minute)`, section 22.2) instead of to whenever the call
happened to start, and by having `Reconcile` re-enter the await/rollback
path whenever the running digest already equals `desired` but `desired !=
proven` -- resuming with whatever time is actually left, immediate
rollback if the deadline has already passed. `now func() time.Time` joins
`sleep` as an injected parameter (still no `Clock` type, still no real
wait in tests).

**R3 -- `runtime.go` had no tests at all.** Fixed with `execCommand`, a
package variable standing in for `exec.Command` (`var execCommand =
exec.Command`) that tests swap for a recording fake -- the exact argv
`Start`/`Status` build is asserted directly (in particular that `--pull
never` is present, and that every refused digest -- empty, `-rm`, wrong
length, non-hex, or already carrying a repo prefix -- never reaches
`execCommand` at all), without ever touching Docker.

**Deeper finding, main session (read while fixing R1-R5): `desired`/
`proven` are registry *manifest* digests, not local image IDs.** Section
13's desired state carries `"digest": "sha256:..."` -- the manifest digest
the agent already checked against the hard-coded sources (security
principle 2) before ever writing the state file. `docker inspect -f
'{{.Image}}'` reports something else entirely: the *local image ID* (a
content hash of the image config), a different value for the same image.
Worse, `docker tag sha256:<manifest digest> ...` does not resolve at all --
only `<repo>@sha256:<manifest digest>` does, and only for an image the
agent actually pulled by digest (so it carries that reference in its
`RepoDigests`). Both `Start` (tagging) and `Status` (comparing the running
digest against `s.Desired`) were wrong as originally written.

**Fixed: a new `-runtime-repo` flag** (default
`ghcr.io/magicalwig34653/thermoctl-agent`) **-- the agent's own hard-coded
image source (security principle 2), never the cloud's to name**, only a
flag default the operator confirms matches `agent/`'s own source list
before shipping an image. `Start` now tags `<repo>@<digest>` (still no
network -- `docker tag` only ever resolves something already local, it
just needed the right reference shape). `Status` now does two `inspect`
calls where it used to do one: the container's running state, restart
count, and local image ID, then (`cliRuntime.repoDigest`) `docker image
inspect -f '{{range .RepoDigests}}{{.}} {{end}}' <image ID>` on that ID,
matching entries against `<repo>@` and returning the manifest digest after
the `@`. No matching entry (an image pulled by tag, or from an unrelated
source) reports an empty digest, which `Reconcile` already treats as
"something other than desired is running" -- never a false positive match.
**Contract clarification for P5.4 (agent-side desired-state
reconciliation), not yet built:** the agent must pull the agent image by
digest (`<repo>@sha256:...`), not by tag, or the running container will
have no matching `RepoDigests` entry and the watchdog can never confirm it
is healthy.

**Line budget: three named functions retired, none of the four required
capabilities lost.** `AgentStopped` and `StartDigest` -- both from the
original P5.6 task -- are gone as separate package-level functions.
Neither survived as *dead* code: `Reconcile` needed the running digest in
the same `Status` call `AgentStopped` would have made on its own (a
second, redundant call to check only the boolean half of the same answer
would have cost more, not less), and `StartDigest` had exactly one call
site once `Reconcile` existed. Both bodies are inlined at that one call
site instead, with a comment at each pointing to why. The underlying
capabilities (section 17 steps 3 and 4) are unchanged and still fully
exercised -- by `Reconcile`'s own test suite now, where before they had
their own direct tests. This was not a line-count-first decision: it was
the reviewer's own two suggested savings (collapsing `Reconcile`'s final
return, reusing `parseOptionalTimestamp` in `ParseHealth`) plus real
restructuring (the deadline-as-poll-count trick from before this round
had to be reverted for R2's sake, which cost lines back) that made it
necessary to look for more, and these two really were dead weight by the
time `Reconcile` existed. Documented here in the spirit of section 22's
introduction, in case a later reader wonders where they went.

Measured with the same counting command as before: `grep -v '^\s*//'
<file> | grep -v '^\s*$' | wc -l`, summed across the seven production
files. **299 statement lines** (unchanged from before this round, net: R1
-5's compose/argv rework and the manifest-digest fix added real weight,
`repoDigest` alone is new; removing the two dead functions and the
reviewer's two suggested savings paid for almost all of it). Per file:
`main.go` 55, `watch.go` 66, `state.go` 39, `health.go` 27, `leds.go` 19,
`linefile.go` 47, `runtime.go` 46. **1 line of headroom before 300** --
P5.7 (LED wiring) will need to trim further, not just add; `linefile.go`'s
`openAndParse` and this round's dead-function removal are the two
precedents to follow first.

**Tests: `go vet ./...` clean, `go test -count=1 ./...` green three runs
in a row, no flake, 46 tests** (up from 41: three `AgentStopped`/
`StartDigest`-specific tests retired along with the functions, replaced by
one `Reconcile`-level status-error test, against eight new ones -- R3's
`runtime.go` argv tests (none existed before this round at all: refusing
every kind of bad digest, tagging `<repo>@<digest>`, `--pull never`
present, resolving a manifest digest out of `RepoDigests`, and reporting
"" on no match), plus R2's two resumed-mid-swap `Reconcile` tests and two
more `AwaitHealthReport` deadline-anchoring tests). `check_contract.sh`
passes unchanged -- neither the state
file nor the health report format changed. `gofmt -l .` empty. Python
side: `ruff check .` clean; `python -m tools.check_image_config` passes
(new `check_agent_compose_file`); `pytest tests/test_watchdog_contract.py
tests/test_image_config.py` 15 passed (up from 13: two new compose-file
plausibility tests).

## P5.6 -- watchdog main loop implemented (`watchdog/watch.go`, `main.go`)

`AgentStopped`, `StartDigest`, `AwaitHealthReport`, `RollBackToProven` are
now actually implemented (section 17, steps 3-6), no longer stubs -- the
scaffold's own "everything here returns an error" note in `watch.go`'s
module docstring is gone.

**A new `Runtime` interface (`watchdog/runtime.go`), not a library.** The
container runtime is addressed exclusively through `os/exec` (section
18.3): `cliRuntime` wraps a configurable `docker`-compatible binary
(`-runtime-bin`, default `docker`) and container name (`-runtime-container`,
default `thermoctl-agent`) -- `Start` does `rm -f` then `run -d --name`,
`Status` a single `inspect -f` printing running-state, image digest, and
restart count in one call. `go.mod` still carries no `require` -- `grep -c
require go.mod` is 0 (the file's own explanatory comment is not a
dependency). Tests substitute a fake `Runtime` and never invoke `docker` at
all.

**No wall clock in tests, and no `Clock` type at all.** `AwaitHealthReport`
and `Reconcile` (new, see below) take a plain `sleep func(time.Duration)`
instead of calling `time.Sleep`, or wrapping it in an interface -- production
passes `time.Sleep` itself as a function value, tests pass a no-op that
only counts calls. The 10-minute deadline from section 17 step 5 is
`maxHealthPolls`, a poll count (`10 * time.Minute / healthPollInterval`)
rather than a wall-clock comparison, so `AwaitHealthReport` needs no notion
of "now" at all -- only the number of times it has slept.

**`Reconcile` (new) ties the four functions into one pass and returns an
`Outcome`** (`OutcomeOK` / `OutcomeRolledBack`) -- the single hook P5.7
needs to drive `leds.go` without touching this decision again per the
task's own instruction. `desired == proven` skips the await/rollback dance
entirely (nothing to swap between): the agent is simply restarted on the
one digest that exists; if that single start itself fails, the failure is
only reported, not rolled back into itself (there is nothing else to try).
When `desired != proven` and the start succeeds, `AwaitHealthReport` is
awaited; an empty returned reason means healthy, otherwise
`RollBackToProven` is called with that reason. An empty `proven` is
reported as an error and nothing is started, exactly as section 22.5
("Fallback without a proven revision") describes it, unchanged from the
existing behaviour.

**Where the rollback reason goes: stderr, not a new file.** Decided in
favour of stderr (captured by journald under the systemd unit) over a new
line-based note file, because a single, one-shot diagnostic line is exactly
what journald is for, and a third line-based file contract would need its
own reader, its own test, and its own entry in `check_contract.sh` for a
value nothing else in the system reads back programmatically. Neither the
state file nor the health report gained a new line -- `check_contract.sh`
is unchanged and still passes.

**Line budget: 299 statement lines, up from 195** (`main.go`, `watch.go`,
`state.go`, `health.go`, `leds.go`, `linefile.go`, plus the new
`runtime.go` -- seven files now), measured with the same counting command
as before: `grep -v '^\s*//' <file> | grep -v '^\s*$' | wc -l`, summed
across the seven files (comments and blank lines excluded, matching
section 18.3's redefined rule). Getting from an initial draft (over 350
statement lines with a full `Clock` interface, three separate configurable
shell-command flags, and a four-way `Outcome`) down to 299 took three
rounds of trimming, documented here because the reasoning is the kind
section 22's introduction asks to keep: (1) replacing `Clock`
(interface with `Now`/`Sleep`) with a plain `sleep func(time.Duration)`
parameter and expressing the deadline as a poll count removed the need for
"now" anywhere in this package; (2) collapsing the three configurable
shell-command strings (`start-cmd`/`status-cmd`/`restarts-cmd`, each a
template with `{digest}` substitution) down to a plain `docker` wrapper
taking only a binary name and a container name removed most of
`runtime.go`; (3) `openAndParse`, a small generic helper added to
`linefile.go` (`func openAndParse[T any](path string, parse func(io.Reader)
(T, error)) (T, error)`), absorbed the "open, defer close, parse" shape
`LoadState` and `ReadHealth` each repeated, the same reasoning that
produced `readKeyValueLines` in the first place. `gofmt -l .` confirmed
empty throughout -- a tempting further trick (writing `if err != nil {
return err }` on one line to save two lines per guard clause) does not
survive `gofmt`, which expands it back to three lines every time; verified
directly before relying on it, and abandoned once disproved.

**Tests: `go vet ./...` clean, `go test -count=1 ./...` green, 41 tests (up
from 25)**, covering: the happy path (stopped agent, desired started,
matching health report arrives, no rollback); a health report that never
arrives (rollback after the full deadline, with the reason); a health
report naming the wrong digest (treated as identically to missing, per
section 22.3); a stale health report older than `since` (same treatment,
section 22.2); three restarts in a row (immediate rollback, no waiting out
the deadline); a runtime failure starting the desired digest (reported, no
rollback, no crash, since `desired == proven` in that test); a runtime
failure during an actual swap (rollback attempted); an empty `proven`
during a swap (error, exactly one start attempt -- the failed one -- no
fallback start); and the agent still running (nothing touched at all).
`check_contract.sh` passes unchanged. `gofmt -l .` empty. Python side
untouched by this task: `ruff check .` clean, `pytest
tests/test_watchdog_contract.py` unchanged at 7 passed.

**P5.7 is next, not part of this task:** `watchdog/leds.go` still has
`LedSetPattern` unimplemented; the `Outcome` return value from `Reconcile`
is this task's documented hook for it, deliberately coarse (two values) --
P5.7's own acceptance criterion (a correct pattern per state) may need
either a finer `Outcome`, or to read `Runtime.Status`/health directly
itself for the LED-specific distinctions (starting vs. healthy vs. no
cloud contact) that section 23.2 draws and `Reconcile`'s outcome does not.

## Cross-review hot fix: P4.2b accepted small-order Ed25519 keys (main
session) **SR**

**The finding, stated plainly.** P4.2b's own cross-review (2026-09-26)
reproduced, end to end, that the signed challenge P4.2b's whole design
exists to require ("answers a signed challenge ... before ... ever trusted
for anything security-relevant", 15.3 step 2/4) could be passed **with no
private key at all**. `cryptography.hazmat.primitives.asymmetric.ed25519
.Ed25519PublicKey.from_public_bytes` loads `bytes(32)` (32 zero bytes)
without error -- that library performs no subgroup/canonicity validation on
a presented public key, by its own documented design, leaving that entirely
to the application -- and `.verify(bytes(64), message)` (an all-zero
"signature") against that "key" then **succeeds for a nontrivial fraction
of arbitrary messages** (the main session measured 4 of 20; the reviewer
measured 20-60% across runs; this module's own test measures it directly
against 30 messages and asserts it is nonzero, so the underlying behaviour
stays proven, not merely remembered). `bytes(32)` happens to be the
canonical encoding of one of Ed25519's eight points of *order dividing 8*
(a "small-order"/"torsion" point) -- for such a key, EdDSA's own cofactored
verification equation is satisfied by a large fraction of arbitrary
signature/message pairs, algebraically, independent of any private key.
Concretely, the attack the reviewer walked end to end: register a device
with `public_key = bytes(32)` (wins whatever registration-code race an
attacker needs to win in the first place, no different from any other
substitution attempt P4.2b was built to catch) -- its verification code
looks like any other, since `verification_code_for` is a plain hash of
whatever bytes it is given, canonical or not -- confirm it in the UI like
any other device, request a challenge, and answer it with an all-zero
signature. A real apartment agent token came back. **This is exactly the
substitution P4.2b's signed-challenge step was built to make impossible**,
defeated by a class of input neither this package's original tests nor its
own review happened to try.

**Fix: `fleet/ed25519_checks.py` (new module, pure Python integers,
stdlib-only -- no `cryptography` import, kept separate from `protocol/`
for the same reason the original package's own arithmetic-adjacent
functions live in `fleet/`, not `protocol/`).** Implements RFC 8032 section
5.1.3's point decoding *exactly as specified*, including both canonicity
checks that section explicitly calls for (`y >= p`; `x == 0` with the sign
bit set) and, critically, the section's own further recommendation ("some
implementations additionally check that the resulting point is not one of
these [eight] points") that P4.2b's original implementation had not applied
at all: `is_low_order_point` computes `[8]P` via three point doublings (the
curve's cofactor) and rejects if the result is the identity -- this is the
one check that actually catches `bytes(32)` (a canonical, on-curve,
order-4 point) and every other one of the eight. The signature's own two
halves get the equivalent treatment (`reject_malleable_signature`): `R`
(the first 32 bytes) through the identical point-decode-and-low-order
check, and `S` (the scalar half) rejected unless strictly less than the
group order `L` (RFC 8032's own "S < L" cofactored-verification
requirement -- the other classical Ed25519 malleability defense, distinct
from but adjacent to the low-order-point issue: without it, `S' = S + L`
verifies identically to `S`).

**Applied in two places, `fleet/app.py`:** `report_device_registration`
(`POST /v1/registration`) rejects a low-order presented public key
*before* `record_device_report` ever stores it -- the same uniform `400`
as every other registration failure, so an attacker learns nothing new.
`request_device_token` (`POST /v1/registration/{id}/token`) re-checks the
**stored** key (defense in depth: a low-order key must never reach
`.verify(...)` even if it had somehow been stored by some path other than
this package's own registration endpoint) and validates both signature
halves, all before `Ed25519PublicKey.from_public_bytes(...).verify(...)`
is ever called -- the same uniform `404`-style refusal as every other
token-endpoint failure.

**Tests, proving the fix and the original finding both, not only
arguing them.** `tests/test_ed25519_checks.py` (new, 18 tests): all eight
of Ed25519's small-order points, in their *canonical* encodings --
**derived programmatically inside the test file** via an independent point-
addition/scalar-multiplication implementation (never hand-copied hex from a
paper, which would itself have been exactly the kind of single-wrong-digit
transcription risk this fix exists to avoid), each individually confirmed
low-order *and* rejected; the specific literature-cited vectors (the
all-zero key, the identity `01 00...00`, and `ec ff...ff 7f`, i.e. `y =
p-1`, matching the exact value the reviewer's own finding cited); two
non-canonical `y >= p` encodings; 200 freshly generated real
`cryptography.Ed25519PrivateKey` public keys, asserted to **all** pass (no
false positives -- the fix must reject only the degenerate cases, never a
genuine key); a real signature passing the malleability check unchanged;
an all-zero signature rejected via its low-order `R`; an `S == L` and an
`S` far above `L` both rejected; and
`test_all_zero_key_and_all_zero_signature_reproduces_the_finding`, which
first *reproduces* the underlying `cryptography` behaviour directly (so
this test would itself fail loudly, not silently pass, if a future
`cryptography` release changed that behaviour) and then confirms both new
checks refuse the pair. `tests/test_device_registration_v1.py` gained two
end-to-end regression tests:
`test_register_low_order_public_key_uniform_failure` (the `POST
/v1/registration` refusal, nothing stored, the registration code stays
usable) and `test_zero_key_and_zero_signature_attack_never_yields_a_token`
(the reviewer's own full attack, replayed against the real endpoints end to
end -- registration refused, and, as defense in depth, a low-order key
inserted directly via `Storage` still cannot obtain a token through
`request_device_token`'s own independent re-check). `fleet/ed25519_checks
.py` is at 100% coverage -- its two truly unreachable lines (`_mod_
inverse`'s zero-denominator guard, reached only if Ed25519's own `d` were
not a non-square mod `p`, which is exactly the algebraic property that
makes this curve's addition law "complete" in the first place) are
`# pragma: no cover` with that reasoning, not silently excluded.

**Open point this fix surfaces, not decided here (P2.3, still
deferred):** the agent side must generate its own Ed25519 key pair only
through a proper, audited library (`cryptography`'s own `Ed25519PrivateKey
.generate()`, or an equivalent) -- never construct or accept a "key" from
anywhere else (a config default, a fixture, an all-zero placeholder during
early bring-up) that could turn out to be low-order. Symmetrically, the
agent itself must never *accept* a low-order key or signature from
anywhere it might one day receive one (e.g. if a future WireGuard-adjacent
key-exchange path reused this class of arithmetic) -- this package's own
fix protects the cloud side of *this* exchange; P2.3's own work order
should read this section before assuming Ed25519 handling is "already
solved" by P4.2b.

Verification (fresh venv, `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"` -- SQLAlchemy 2.1.1, mypy 2.3.1, cryptography
50.0.1): `ruff check .`, `mypy .`, `mypy protocol fleet agent tools` all
clean; `python -m pytest -W ignore::ResourceWarning` (**725 passed**, 99%
coverage overall, `fleet/app.py`/`fleet/storage.py`/`fleet/ed25519_checks
.py`/`protocol/registration.py` all at 100%);
`test_concurrent_token_requests_exactly_one_token_wins` and
`test_register_throttle_429_reserve_then_verify` each re-run 10x in a row,
no flake. `watchdog/`: `go vet ./...` clean, `go test ./...` green, `bash
watchdog/check_contract.sh` passes (re-run for completeness; this fix
touches no `protocol/` field, only adds a new `fleet/`-internal module and
two call sites).

## Device-side registration: Ed25519 + signed challenge (P4.2b, sections 4,
14, 15.3) **SR**

**Decision by the project owner, 2026-09-26 (do not re-open, see this
package's own work order):** device registration uses **Ed25519 and a
signed server challenge**. The verification code shown on the device and in
the fleet UI is **derived from the public key's own fingerprint**
(`protocol.registration.verification_code_for`), so a substituted key
always shows a different code -- P4.2's own confirmation step (constant-
time comparison, `Storage.confirm_device`) is unchanged, only what value it
is ever asked to compare against changed, from "whatever the device
happened to send" to "a value nobody, including the device's own firmware
bug, can pick". The apartment's agent token is issued **only** for a
registration confirmed in the UI, bound to exactly the stored public key --
never before that confirmation, never to a different key.

**Flow (15.3 steps 2-4), device side simulated by `tests
/test_device_registration_v1.py`'s own helpers (`cryptography`'s
`Ed25519PrivateKey`, generated fresh per test, never a literal):**

1. `POST /v1/registration` (`RegistrationRequest`) -- the device's one-time
   registration code (from P4.2's "prepare") plus its own Ed25519 **public**
   key. `fleet.app.report_device_registration` validates the key is a real
   Ed25519 public key (`cryptography.hazmat.primitives.asymmetric.ed25519
   .Ed25519PublicKey.from_public_bytes`, `fleet` extra only -- never
   imported by `protocol/`), computes the verification code **server-side**
   from the key (`verification_code_for`, never trusted from the caller --
   there is no such field on `RegistrationRequest` to trust from in the
   first place), and calls `Storage.record_device_report` (P4.2's own,
   unchanged entry point). Success -> `201 RegistrationAccepted
   {registration_id}` -- a random, unguessable id
   (`Storage.assign_registration_external_id`, `secrets.token_urlsafe(24)`),
   **never** the row's own sequential database id (would let a caller
   enumerate other devices' in-progress registrations). **Every failure is
   one uniform `400` response** (`_uniform_registration_failure`): unknown/
   expired/invalidated/already-used code, a malformed or structurally
   invalid public key, and a decommissioned device (P4.2's own guard) are
   all indistinguishable.
2. `POST /v1/registration/{registration_id}/challenge` -- the device's own
   polling loop (section 3's own 60-second cadence, documented via a
   `Retry-After: 60` header on the `202` response). Not yet confirmed ->
   `202`, empty body. Confirmed -> `200 TokenChallenge {nonce, expires_at}`
   -- a fresh, single-use nonce (>=32 raw bytes, `secrets.token_bytes`,
   encoded per `protocol.registration`'s own convention), stored **hashed**
   (`Storage.issue_token_challenge`), 5-minute expiry
   (`Storage._TOKEN_NONCE_VALID_MINUTES`), overwriting any earlier nonce for
   the same row (single active nonce per registration, the same "single-
   use applied at the row level" reasoning as the registration code
   itself). Unknown/invalidated/already-token-issued `registration_id` ->
   the same uniform `404`-style refusal in all three cases.
3. `POST /v1/registration/{registration_id}/token` (`TokenRequest{nonce,
   signature}`) -- the device signs a **domain-separated** message,
   `b"thermoctl-fleet/token/v1\0" + registration_id + b"\0" + nonce`, with
   its Ed25519 private key (never leaves the device) and echoes the nonce
   back alongside the signature. `fleet.app.request_device_token` verifies
   the signature against the **stored** public key (never one the request
   itself supplies) before ever touching storage; `Storage
   .issue_device_token` then does everything else -- nonce consumption,
   every remaining precondition (confirmed, not invalidated, token not
   already issued, the device still holds the open assignment created at
   confirmation, the apartment not retired), the actual token generation
   (`agent_<apartment>_<random>`, >=32 bytes of entropy, section 4), and the
   apartment's `token_hash` write -- **in one guarded transaction**, so a
   failure or a lost race leaves nothing applied. Success -> `200
   TokenIssued {token}`, returned **exactly once**; every other outcome
   (wrong/malformed signature, a signature over a different registration id
   or a stale/expired/reused nonce, a second attempt after success, no open
   assignment, a retired apartment) is the **same uniform `404`-style
   response**.

**Encodings, documented once in `protocol/registration.py`'s own module
docstring, used by both sides identically:** a raw Ed25519 public key (32
bytes), a raw Ed25519 signature (64 bytes), and a raw nonce (>=32 bytes) are
all base64url-encoded **without padding** (`encode_bytes`/`decode_bytes`,
pure stdlib `base64`/`hashlib` -- `protocol/` stays pydantic+stdlib only,
`cryptography` is never imported there, only in `fleet`'s own extra).
`decode_bytes` reimplements `base64.urlsafe_b64decode`'s own `-`/`_`
translation and calls `base64.b64decode(..., validate=True)` directly --
plain `urlsafe_b64decode` does **not** itself validate and silently
*discards* out-of-alphabet characters instead of rejecting them, which
would have let a malformed string slip through as if properly encoded
(caught while writing `tests/test_registration_protocol.py
::test_decode_bytes_rejects_invalid_base64url`, not merely assumed correct).

**`verification_code_for` (`protocol/registration.py`):** SHA-256 of the
raw public-key bytes, truncated to the first 40 bits (5 bytes), rendered as
8 Crockford-base32 symbols (no `I`/`L`/`O`/`U`, chosen so a human reading it
off a small screen cannot confuse `0`/`O` or `1`/`I`/`L`), formatted
`XXXX-XXXX`. **40 bits is enough for what this code actually has to do, not
in general:** it is a human-eyeball comparison of two short strings shown on
two screens at once, not the security boundary itself -- that is the signed
challenge that follows *after* confirmation, verified against the exact key
this code was derived from; a substituted key producing a colliding
fingerprint (a 2**40 search) still cannot answer that challenge with the
legitimate device's own private key. Deterministic, differs for different
keys, format-tested directly (`tests/test_registration_protocol.py`).

**Protocol additions, purely additive (section 18.2).** `PROTOCOL_VERSION`
was *not* bumped at the time this section was originally written (the
argument then: a bump is only required for a changed field name, a changed
required field, or a changed meaning of an existing field -- every change
here is a brand-new model or a new pure function; no existing model's
fields changed at all). **Superseded, see "`PROTOCOL_VERSION` bump to 2"
below: the project owner has since read section 18.2 literally, and these
four models are exactly what bumped it to 2.** `RegistrationAccepted
{registration_id}`, `TokenChallenge{nonce, expires_at}`, `TokenRequest{nonce,
signature}`, `TokenIssued{token}` (no `examples=`/default value on `token`
-- CLAUDE.md: "no secrets in the repo, not even as a real-looking example
value", applied to a field instead of a whole model this time).
`tests/test_registration_protocol.py
::test_no_protocol_model_field_name_ever_mentions_a_private_key` walks
every field name of every Pydantic model importable from `protocol` (plus
every model defined directly in `protocol.registration`) and asserts none
contains "private" -- CLAUDE.md security principle 3, checked directly, not
only argued.

**Schema (`fleet/migrations/versions/0008_device_registration_tokens.py`,
`down_revision` `"0007"`).** Four new, nullable columns on P4.2's own
`device_registrations` table (`external_id` -- unique, indexed, the
device-facing id described above; `token_nonce_hash`/`token_nonce_expires_
at`/`token_nonce_consumed_at` -- the current challenge's nonce, hashed,
never stored raw, mirroring `code_hash` exactly) and one new, sibling
table, `device_registration_throttle` -- **not** the same table as P3.0's
`ui_login_throttle`: this one throttles three *independent* request kinds
per IP (see below), each needing its own budget, so its primary key is the
pair `(ip, purpose)`, not the IP alone.

**Per-IP throttle, reserve-then-verify (the exact P3.0 round-4 pattern,
`Storage.reserve_registration_throttle`/`release_registration_throttle`),
checked *before* any code/registration lookup, three independently
configured "purposes":**

| Purpose | Endpoint | Default threshold | Default window/block | Env vars |
|---|---|---|---|---|
| `register` | `POST /v1/registration` | 10 | 15 min / 15 min | `FLEET_REGISTRATION_THROTTLE_{THRESHOLD,WINDOW_S,DURATION_S}` |
| `challenge` | `.../challenge` | **30** | 15 min / 15 min | `FLEET_REGISTRATION_CHALLENGE_THROTTLE_{THRESHOLD,WINDOW_S,DURATION_S}` |
| `token` | `.../token` | 10 | 15 min / 15 min | `FLEET_REGISTRATION_TOKEN_THROTTLE_{THRESHOLD,WINDOW_S,DURATION_S}` |

**Why the challenge endpoint's default is far more generous, not an
oversight:** section 3's own 60-second poll cadence applies here too (a
device waits for the landlord's UI confirmation by polling roughly once a
minute) -- a tight, login-sized budget would let a legitimate, well-behaved
device throttle *itself* purely by waiting. Every `200`/`202` response
additionally **releases its own reservation** (`release_registration_
throttle`, the same "give a legitimate attempt's budget back" reasoning
P3.0's login throttle already established) -- in practice a well-behaved
device never spends its budget down at all; only the uniform-refusal branch
of each endpoint consumes it. Proven under real concurrent threads, not
only argued: `tests/test_device_registration_v1.py
::test_register_throttle_429_reserve_then_verify` (10 concurrent requests,
threshold 3 -- exactly 3 reach the "unknown code" check, exactly 7 get
`429` before any lookup at all) and `::test_challenge_throttle_429`.

**Races/guards, proven under real threads, not only argued:**
`tests/test_device_registration_v1.py
::test_concurrent_token_requests_exactly_one_token_wins` (10 threads, one
valid signature+nonce, re-run 10x during verification with no flake) --
exactly one `200`, nine `404`; the nonce-consumption and the token-issuance
guard are the *same* `UPDATE ... WHERE ...` statement
(`Storage.issue_device_token`), so no two concurrent callers can both
observe "still valid" for what turns out to be the second winner.
`::test_token_after_remove_device_refused` -- a device racing (here:
preceding) a `remove_device` call never gets a token, since the open-
assignment `EXISTS` subquery is folded into that same guarded statement, not
a separate read beforehand. `::test_challenge_after_invalidation_uniform_
404` -- a manual transition out of `reported` (P4.2/P4.3's own registration-
invalidation rule) makes a still-pending challenge unreachable. `::test_key_
substitution_confirm_with_expected_code_fails` -- device B registering with
device A's code produces a *different* stored verification code (derived
from B's key), so confirming with the code that would have belonged to A's
own key fails `confirm_device`'s existing constant-time comparison, exactly
the substitution-detection property this package's whole design exists for.

**Never logged, never stored raw, proven directly, not only by
inspection:** `tests/test_device_registration_v1.py
::test_raw_token_never_stored_or_logged` asserts the issued token is absent
from `caplog`'s captured text *and* from the raw bytes of the SQLite file on
disk -- only `hash_token`'s digest is ever persisted
(`ApartmentRecord.token_hash`), the same pattern every other agent token in
this codebase already follows. Registration codes, verification codes,
nonces, and signatures are likewise never logged anywhere in `fleet/app.py`
or `fleet/storage.py`'s new code. `Cache-Control: no-store` on all three
endpoints' responses, success and failure alike.

**`fleet` extra gained `cryptography>=43`** (`pyproject.toml`) -- validating
a device's Ed25519 **public** key and verifying its signature; never used
to *generate* or hold a private key anywhere in the cloud (CLAUDE.md
security principle 3) -- that only ever happens in `agent/` (P2.3, still
deferred). `protocol/` itself imports neither `cryptography` nor anything
from `fleet`/`agent` -- verified by `protocol/registration.py`'s own module
docstring reasoning and by every new function there being pure `base64`/
`hashlib`.

**Tests.** `tests/test_registration_protocol.py` (new, 15 tests): `encode_
bytes`/`decode_bytes` round-trip, URL-safety, padding, and the invalid-
base64url rejection above; `verification_code_for`'s determinism, format
(Crockford alphabet, no `I`/`L`/`O`/`U`), and "differs for different keys";
the new models' basic validation; the protocol-wide "no field ever
mentions a private key" walk. `tests/test_device_registration_v1.py` (new,
26 tests, against a real, migrated SQLite database and a real
`TestClient(app)`): the full happy path end to end (register -> `202`
before confirm -> UI confirm via `Storage.confirm_device` -> `200` challenge
-> token -> a real `POST /v1/heartbeat` with the issued token -> `204`);
wrong-length/not-base64/unknown/reused/invalidated registration codes, all
uniform `400`s; the expired-code case at the storage level with an injected
clock (mirroring P4.2's own convention for this class of test); the
register/challenge throttles (concurrent and single-shot); challenge before
confirm (`202`)/unknown (`404`)/after invalidation (`404`); wrong signature,
signature over a different `registration_id` (domain separation), a stale
(overwritten) nonce, a malformed (not base64) signature, and an expired
nonce at the storage level with an injected clock -- all refused, no token;
the concurrent-token-request race; token after `remove_device`; a second
token attempt after success; the issued token working on a real heartbeat
and getting `403` after `remove_device`; the raw-token-never-stored-or-
logged proof; two direct `Storage` edge cases
(`assign_registration_external_id`'s "no active registration"/idempotent-
retry branches, `issue_token_challenge` after the token was already issued,
`issue_device_token` for an unknown `external_id`) reached that no HTTP-
level test could reach on its own. `fleet/app.py`, `fleet/storage.py`
(P4.2b's own additions), and `protocol/registration.py` are all at 100%
coverage for this round -- the two remaining `# pragma: no cover` lines in
`fleet/storage.py`'s new code are the same class of narrow, single-writer-
transaction race P4.2's own rechecks already document (an artificial second
writer would be needed to hit them deterministically), not an untested real
path.

**Open points, left for later packages, not invented here:**

- **Token rotation is not built.** `Storage.issue_device_token` refuses a
  second call outright ("token already issued... a new device registration
  is needed"); the specification's own "the cloud can issue a new token"
  (section 4) needs a dedicated rotation flow for an *already*-in-service
  apartment, which is out of this package's scope (P4.2b only ever issues
  the *first* token for a freshly confirmed registration).
- **The agent side (P2.3, still deferred) is not built here** -- key
  generation, storing the private key on the base station, calling these
  three endpoints, and persisting the issued token are all still to do
  once thermoctl's own `/api/v1/health` exists (see P2.3's own entry in
  `docs/implementation_plan.md`). This package's tests play the device's
  role with a throwaway `cryptography.Ed25519PrivateKey` generated at
  runtime, never a real agent.
- **TLS certificate pinning on the agent (section 4: "the agent knows the
  cloud's expected fingerprint... as a second barrier") is P2.3's job, not
  this one.** `AgentRegistrationFile.certificate_fingerprint` already
  exists (P4.2) for exactly this purpose; nothing in this package checks it
  from the cloud side, since pinning is inherently a client-side (agent)
  concern the cloud cannot enforce on itself.

Verification (fresh venv, `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"` -- SQLAlchemy 2.1.1, mypy 2.3.1, cryptography 50.0.1):
`ruff check .`, `mypy .`, `mypy protocol fleet agent tools` all clean;
`python -m pytest -W ignore::ResourceWarning` (705 passed, 99% coverage
overall, `fleet/app.py`/`fleet/storage.py`/`protocol/registration.py`/the
new migration all at 100%); `test_concurrent_token_requests_exactly_one_
token_wins` and `test_register_throttle_429_reserve_then_verify` each
re-run 10x in a row, no flake. `watchdog/`: `go vet ./...` clean, `go test
./...` green, `bash watchdog/check_contract.sh` passes (`protocol/`
changed, so the Go track was run per this package's own verification
instructions -- `watchdog/` itself was not touched, and the contract test
does not exercise anything from `protocol/registration.py`, which never
crosses into the watchdog's own state file).

## Cross-review integration: P4.2 x P4.3 device lifecycle (main session)

P4.2 ("prepare device, confirm registration and assign") and P4.3 ("remove/
replace device, change device state") were built in parallel on separate
branches and merged together here. P4.3's own STATUS.md section already
flagged the gap this closes: "decommissioning must also invalidate any
pending registration ... not built here ... the main session should add
this once both branches are merged together." This section is that
hand-off, done.

**Extended `ALLOWED_MANUAL_DEVICE_TRANSITIONS` (`fleet/device_lifecycle.py`),
a further derived reading of section 20, not a specification quote** (see
that module's own "Extended by P4.2/P4.3 cross-review integration"
docstring section): six transitions beyond P4.3's original five --
`in_storage -> faulty` (the symmetric counterpart P4.3 deliberately left
out, no longer necessary to exclude now that the registration side effect
below exists to invalidate anything "in progress"), `registered -> faulty`,
`prepared -> in_storage`, `prepared -> faulty`, `reported -> faulty`, and
`reported -> decommissioned`. Every other refusal from P4.3's original
table (terminal `decommissioned`, no manual path into `prepared`/
`reported`/`in_service`, `in_service` never a source) is unchanged.
`tests/test_device_lifecycle.py::test_exhaustive_7x7_transition_table`'s
independent expected set was updated to include exactly these eleven pairs,
not the original five -- still parametrized over all 49, still asserting
every non-listed pair is refused.

**Every manual transition that leaves `prepared`/`reported`, or moves into
`decommissioned`, invalidates the device's active registration in the same
transaction as the state change, and audits it separately.**
`Storage.change_device_state` (previously P4.3-only) now also calls a new
`Storage._invalidate_active_registration` helper (P4.2's own transaction-
scoped, audited invalidation pattern, reused rather than re-implemented)
whenever `before_state in ("prepared", "reported") or target_state ==
"decommissioned"` -- covers all eleven allowed transitions without
enumerating each one, since every new transition satisfies at least one
half of that condition (`reported -> decommissioned` satisfies both).
Writes its own `"registration_invalidated"` audit row (`entity_type=
"device"`), separate from the `"state_changed"` row the state change itself
already writes -- two kinds of change, two rows, same reasoning
`remove_device`'s own three-rows-for-three-changes convention already
established. A no-op (nothing touched, nothing logged) when there is no
active registration to invalidate in the first place (e.g. a `registered ->
faulty` device that was never prepared).

**`record_device_report` additionally refuses a `decommissioned` device**,
folded into its own existing guarded `UPDATE` as a `NOT EXISTS` subquery on
`devices.state`, not a separate check-then-act read -- defense in depth on
top of `change_device_state`'s own invalidation (a registration should
already be invalidated by the time a device reaches `decommissioned`, but a
second, independent guard here means a future code path that somehow
reaches `decommissioned` without going through `change_device_state` still
cannot be reported against). `confirm_device` needed no equivalent change:
its own rule 1 ("device must be `reported`, with an active registration")
already refuses a `decommissioned` device structurally -- `decommissioned`
is a different value than `reported`, and `change_device_state` always
invalidates the active registration on that exact transition, so both
halves of rule 1 already fail together.

**Proven, not just argued (`tests/test_device_lifecycle_registration_
integration.py`, new):**

- `tests/test_device_lifecycle.py`'s exhaustive table test, updated (11
  allowed pairs, 38 refused, all 49 checked).
- Leaving `reported` (manually, to `faulty`) invalidates the registration,
  so a later `confirm_device` call fails (the device is no longer
  `reported`, checked before the registration itself ever is) and
  `record_device_report` with the *old* code also fails -- both against
  the same registration, both after nothing but a manual state change
  touched it. Leaving `prepared` (to `in_storage`) invalidates a
  still-unused code the same way. Decommissioning a `prepared` device
  makes its still-unused code unusable (`record_device_report` returns
  `False`), with both a `"registration_invalidated"` and a `"state_changed"`
  audit row to show for it. `registered -> faulty` (a device that was
  never even prepared) writes no spurious invalidation row -- there is
  nothing to invalidate. Both `prepare_device` and `confirm_device` refuse
  a `decommissioned` device outright (separate tests, not only inferred
  from the state-check above), and `record_device_report` refuses one even
  when the registration row itself was left untouched (its own independent
  `NOT EXISTS` guard, defense in depth on top of `change_device_state`'s
  invalidation). A concurrent manual `reported -> faulty` racing a
  `confirm_device` call with the correct code -- run 10x under real
  threads -- always produces exactly one outcome: either the confirm wins
  and ends with a device correctly `in_service` and a *not*-invalidated
  registration (confirmed, not invalidated -- the two are mutually
  exclusive terminal states for one registration row), or the manual state
  change wins and the confirm correctly fails -- never both, never an
  `in_service` device whose own registration row is also marked invalidated
  (asserted explicitly every run).
- A `sqlite_master` assertion
  (`tests/test_device_lifecycle_registration_integration.py
  ::test_migration_0007_partial_unique_index_carries_the_where_clause`)
  that `ux_device_registrations_device_id_active` actually carries `WHERE
  invalidated_at IS NULL AND confirmed_at IS NULL` in the stored schema --
  mirrors P4.1's own equivalent assertion for the assignments indexes; the
  existing concurrency tests already prove the index *behaves* as partial,
  this additionally proves the stored schema says so, not just that test
  data never happened to hit the non-partial case.

**Review suggestion, also fixed here: `confirm_device`'s `replace_previous`
path closed the previous assignment via a bare ORM attribute set
(`previous_assignment.ended_at = normalized_now`), not an atomically
guarded `UPDATE`.** Harmless before P4.3 existed (nothing else could ever
close that same assignment concurrently); **P4.3's own `remove_device` now
can** -- a landlord confirming a replacement device for apartment X at the
same moment as (or via a stale page, slightly after) someone else clicks
"Gerät ausbauen/tauschen" for the very same apartment's *old* assignment
would previously have let `confirm_device` silently re-close and re-audit
an assignment `remove_device` had already closed and audited, without
detecting the collision. Fixed: the close is now `UPDATE ... WHERE id =
<this assignment> AND ended_at IS NULL`, exactly `remove_device`'s own
"atomic close, not read-then-write" pattern; a lost race raises `ValueError`
("wurde inzwischen bereits anderweitig beendet") instead of silently
double-processing. `tests/test_device_lifecycle_registration_integration.py
::test_confirm_device_replace_previous_races_a_concurrent_remove_device`
proves it directly: `remove_device` and `confirm_device` (with
`replace_previous=True` for the same apartment/previous device) run
concurrently, real threads -- exactly one of the two "closes" the
assignment; the other gets a clean `ValueError`, and the audit log has
exactly one `"closed"` row for that assignment, never two.

**P4.2b's own open points, made explicit here (not decided, not invented --
this package's own boundary, restated so P4.2b's work order does not have
to rediscover it by reading every method's docstring):**

- **Rate limiting on the future `/v1/registration/...` report endpoint is
  P4.2b's decision, not made here.** `Storage.record_device_report` itself
  is already safe under concurrency (proven above and in P4.2's own
  section) and already returns a uniform `False` for every failure reason,
  but neither of those is a rate limit -- nothing here throttles how many
  *distinct* codes an attacker may try per unit time against that future
  endpoint (unlike the UI login path's per-IP throttle, P3.0). P4.2b should
  decide whether/how to add one before that endpoint goes live.
- **Verification-code derivation and entropy is P4.2b's decision, not
  made here.** This package stores whatever string `record_device_report`
  is given as `verification_code` and compares it in constant time; it does
  not derive it, does not constrain its length or charset beyond the
  column's own `String(64)` bound, and makes no claim about how much
  entropy a "derived from the key fingerprint" value actually carries
  against a brute-force *within* the 5-attempt budget. P4.2b's own work
  order needs to size that derivation deliberately, not inherit a default
  from this package.
- **Token issuance must only ever happen for a confirmed, non-invalidated
  registration bound to its stored public key -- not built here, stated as
  the contract P4.2b must uphold.** `DeviceRegistrationRecord.token_issued_
  at` exists and is always `NULL` in this package; P4.2b is the only future
  code path expected to ever set it, and it must do so only after checking
  `confirmed_at IS NOT NULL`, `invalidated_at IS NULL`, and that the
  device's own subsequent request is signed by the exact key whose public
  half is stored in `public_key` on that same row (the signed-challenge
  step, section 15.3 step 2's "answers a signed challenge ... before ...
  ever trusted for anything security-relevant") -- issuing a token off a
  registration that was later invalidated (e.g. by a manual
  `change_device_state` decommission, see above) or against a *different*
  key than the one actually confirmed would defeat the entire point of
  "only this confirmation releases the configuration -- bound to the key of
  exactly this device" (15.3 step 3).

**A second, genuine bug found while writing the concurrency test above, not
only the one asked for -- both `confirm_device` and `change_device_state`
wrote their own device's `state`/registration `confirmed_at` via bare ORM
attribute sets, not atomically guarded `UPDATE`s.** Reproduced directly
(`tests/test_device_lifecycle_registration_integration.py
::test_concurrent_confirm_racing_manual_reported_to_faulty_exactly_one_
outcome`, before the fix below): a device ended up `in_service` **and**
its own registration row **also** `invalidated_at`-set at the same time --
exactly the "never both" invariant this whole feature exists to prevent,
because SQLite only serialises two concurrent write transactions at the
point of each one's *first* write statement, not at its first *read* --
a plain `record.state = "in_service"` (or `registration.confirmed_at =
...`) written well after this method's own initial recheck can silently
overwrite whatever a concurrent transaction committed in between, since
nothing about that later write re-verifies the row is still in the state
this method believes it is. **Fixed**: the device-state write and the
registration-confirm write in `confirm_device`'s phase 2, and the
device-state write in `change_device_state`, are now all atomically
guarded `UPDATE ... WHERE <column> = <the value just read>` statements (the
same "the check and the write must be the same statement" pattern this
module already applies throughout -- `record_ui_login_success`,
`_record_wrong_verification_code_attempt`, `remove_device`'s assignment
close) -- a lost race now raises a clear `ValueError` instead of silently
corrupting the row. Two further deterministic tests
(`test_confirm_device_registration_claim_fails_if_invalidated_mid_call`,
`test_change_device_state_guard_fails_if_state_changes_mid_call`) force
each specific guard's own refusal branch via a same-transaction injected
side effect (documented in each test's own docstring as the honest
alternative to a genuinely concurrent write, which would simply deadlock
against this call's own still-open write transaction) -- both guard
branches are covered on every run, not only probabilistically across the
real-thread race test's own repeated runs.

Verification for this round (fresh venv, `python3.13 -m venv`,
`pip install -e ".[dev,fleet,agent]"` -- SQLAlchemy 2.1.1, mypy 2.3.1):
`ruff check .`, `mypy .`, `mypy protocol fleet agent tools` all clean;
`python -m pytest -W ignore::ResourceWarning` (664 passed, 99% coverage
overall, every touched file -- `fleet/storage.py`, `fleet/device_lifecycle
.py`, `fleet/ui_inventory.py`, `fleet/ui_routes.py` -- at 100%); every
concurrency test in the suite (21 total: P4.2's four, P4.3's one, and this
round's new ones -- the manual-transition-vs-confirm race, the
replace_previous-vs-remove_device race, plus the two deterministic
guard-branch tests) re-run 10x with no flake.

## Prepare device, confirm registration and assign (P4.2, sections 4, 15.3, 20.2, 20.3)

**Decisions by the project owner, 2026-09-26 (do not re-open, see this
package's own work order):** device-side registration itself (Ed25519 key
pair, the signed challenge that proves possession of the private key) is
P4.2b, not this package -- this package only builds the storage-level state
both P4.2b and the landlord-facing "prepare"/"confirm" UI forms act on, plus
those two forms themselves (`/ui` routes, P3.0 login + CSRF, same as every
other inventory action since P4.1). `protocol/` was not touched -- no new
field was needed; the registration state lives entirely in `fleet/storage.py`
and never crosses the wire as its own model. `fleet/auth.py`, `fleet
/ui_auth.py`, `watchdog/`, and `protocol.commands.CommandType` are all
untouched, per the work order's own constraint.

**Schema (`fleet/migrations/versions/0007_device_registrations.py`,
`down_revision` `"0006"`).** One new table, `device_registrations` --
**one row per preparation cycle**, not one row per device, so the full
history of every attempt stays queryable (via `inventory_audit_log` for the
device-level actions, and directly on this table for the rest), never
overwritten in place: `device_id` (no `ForeignKey`, mirroring
`assignments.device_id`/`apartment_id`'s own established "plain indexed
string column, not a declared FK" pattern from `0006_inventory.py`),
`code_hash` (SHA-256 of the one-time registration code -- **the code itself
is never stored**, mirroring `apartments.token_hash`/`hash_token` exactly),
`created_at`/`expires_at` (24 hours, section 4), `used_at` (set once by
`record_device_report`), `public_key`/`verification_code`/`reported_at`
(filled together by `record_device_report` -- P4.2b's own entry point, see
below), `confirmed_at`/`confirmed_by`/`apartment_id` (filled by
`confirm_device`), `failed_confirmation_attempts` (incremented on a wrong
verification code), `invalidated_at` (set either by a later `prepare_device`
call superseding this row, or by `confirm_device` after the fifth wrong
attempt), `token_issued_at` (filled by P4.2b once it actually issues a
token against this confirmed registration -- always `NULL` in this package,
since that package does not exist yet). **"At most one active preparation
per device", enforced at the database level** (work order's own explicit
instruction): a partial unique index on `device_id` `WHERE invalidated_at
IS NULL AND confirmed_at IS NULL` -- "active" deliberately does *not* also
exclude an expired-but-not-yet-invalidated row, since `prepare_device`
always invalidates any earlier active row in the same transaction before
inserting a new one, so this index never actually has to arbitrate between
two rows both claiming to be current; expiry itself is checked at read time
(`record_device_report`/`confirm_device`), the same "derived, not enforced
via a background job" choice `HeartbeatRecord`'s "outdated version" flag
already made (P1.3).

**Storage (`fleet/storage.py`, new "-- device registration: prepare / report
/ confirm --" section), all unit-tested directly against a real, migrated
SQLite database, including under concurrency:**

- **`prepare_device(device_id, *, ui_username, confirmed_reset, now)`** --
  eligible from `registered` or `in_storage` only (any other state raises
  `ValueError`); **`in_storage` additionally requires `confirmed_reset=True`**
  (section 20.3 rule 2's "explicit confirmation" applied at the point a
  device's slate is wiped for a new registration cycle -- the "Gerät wurde
  zurückgesetzt" form checkbox, never silently assumed). Invalidates any
  earlier active preparation for the device in the same transaction, moves
  the device to `prepared`, and returns the raw registration code **exactly
  once** -- only its SHA-256 hash is ever persisted.
- **`record_device_report(registration_code, public_key, verification_code,
  now)`** -- P4.2b's own entry point (15.3 step 2), **implemented here so
  that package only has to add the HTTP/crypto layer on top**. One-time:
  marks the code used, stores the public key and verification code, moves
  the device to `reported`. **Every failure is indistinguishable to the
  caller** (a plain `bool`) -- unknown, expired, invalidated, and
  already-used codes all return `False`, never a message that could tell
  an attacker which reason applies. **Atomic under concurrency**: the guard
  (`used_at IS NULL`, not invalidated, not expired) is folded into the
  `UPDATE ... WHERE ...` itself (mirroring `record_ui_login_success`'s "the
  check and the write must be the same statement"), not a separate `SELECT`
  beforehand -- proven with 8 threads reporting the same code concurrently:
  exactly one gets `True`, the other seven `False`.
- **`confirm_device(device_id, apartment_id, verification_code, *, ui_user,
  reason, replace_previous, previous_device_target_state, now)`** -- "only
  this confirmation releases the configuration" (15.3 step 3). Rules
  enforced, in order: device must be `reported` with an active registration;
  **verification code compared with `hmac.compare_digest`** (constant
  time); apartment must exist and not be `retired`; **a device with an open
  assignment elsewhere can never be confirmed** (checked explicitly, not
  only relied upon via the partial unique index further down); if the
  apartment already has an open assignment, confirmation requires
  `replace_previous=True` **and** a valid `previous_device_target_state`
  (`faulty` or `in_storage`) -- "never silently" (section 20.3 rule 1).
  **A wrong verification code increments `failed_confirmation_attempts` and,
  after the fifth wrong attempt (`Storage._MAX_CONFIRMATION_ATTEMPTS = 5`,
  not specified numerically by the specification -- decided here, per the
  work order's own "document the number" instruction), invalidates the
  registration** -- the device must be prepared again. **That increment is
  committed as its own, separate transaction** (`_record_wrong_verification_
  code_attempt`), independent of the overall call's `ValueError` --
  otherwise the counter's own commit would be rolled back by the same
  `raise` it exists to survive. Proven atomic under 10 concurrent wrong
  attempts against one registration: the counter reaches exactly 5, never
  more, never fewer, and the registration is invalidated exactly once. Once
  the code is right, **the rest of the confirmation happens in one
  transaction**: closes the previous assignment (`ended_at`/`reason`), moves
  the previous device to the chosen state, **revokes the apartment's token**
  (`token_hash = NULL`, section 15.5's "its token expires"), creates the new
  assignment (the P4.1 partial unique indexes are the database-level guard
  against a double assignment), moves this device to `in_service`, and marks
  the registration confirmed -- **every step audit-logged in that same
  transaction**. Proven with a real `/v1/heartbeat` request using the old
  (now revoked) token after a replace-previous confirm: 403, not just an
  inspected column. Proven atomic under a genuine race, not only argued:
  two `reported` devices confirmed concurrently to the same (so far
  unassigned) apartment -- exactly one wins (the partial unique index on
  `assignments.apartment_id` decides it at the new assignment's own insert,
  caught as an `IntegrityError` turned `ValueError`), and the loser's own
  transaction rolled back in full: device still `reported`, registration
  still unconfirmed, no `state_changed` audit row written for it.
- **`get_active_registration_for_device`**/**`get_current_assignment_for_
  device`** -- small, direct read helpers the UI and `confirm_device` share.

**A few defensive rechecks inside `confirm_device`'s second transaction
(device/registration/apartment re-fetched and re-validated even though
phase 1 already checked all three) are marked `# pragma: no cover`, each
with its own comment explaining why**: they guard against a narrow race in
the gap between this call's own read-only phase 1 and its write phase 2
(e.g. a concurrent wrong-code attempt invalidating the registration, or an
apartment retired mid-flight) that SQLite's own transaction timing makes
impractical to hit deterministically without an artificially injected pause
between the two phases -- CLAUDE.md's own "a line only reachable through an
artificial construction" reasoning. The two *reachable* races (the wrong-
verification-code counter, and two devices/the same device confirmed to one
apartment concurrently) are proven with real concurrent threads, not
skipped -- see above.

**UI (`fleet/ui_routes.py`, `fleet/ui_inventory.py`, `fleet/templates/ui/
inventory_device_{prepare,prepared,confirm}.html`), behind `require_ui_user`,
CSRF-checked on every POST, `Cache-Control: no-store`, no inline
style/script, the registration code never logged (checked directly via
`caplog`) and never stored in plain text:**

- **`GET`/`POST /ui/inventory/devices/{id}/prepare`** -- the "Vorbereiten"
  form/result. The result page shows the raw code **exactly once**, plus the
  content for `agent-registration.json` (section 15.3/19.5:
  `protocol.registration.AgentRegistrationFile`'s `fleet_address`,
  `certificate_fingerprint`, `registration_code`) -- **address and
  fingerprint come from two new environment variables, `FLEET_PUBLIC_URL`
  and `FLEET_CERT_FINGERPRINT`, deliberately with no default** (CLAUDE.md:
  "nothing hard-coded"; no plausible placeholder exists that would not
  itself look like a real deployment's configuration). If either is unset,
  the page shows a clear German hint instead of inventing a value -- proven
  by `tests/test_ui_device_registration.py
  ::test_prepare_submit_without_fleet_env_vars_shows_a_hint`.
  `fleet/inventory.html`'s device listing gained a "Vorbereiten" link per
  eligible device (`registered`/`in_storage` only).
- **`GET /ui/inventory/devices/confirm`** -- the "Bestätigen" listing:
  every `reported` device, with a *display* fingerprint
  (`fleet.ui_inventory._display_fingerprint`, `hashlib.sha256(public_key)`
  truncated -- a human eyeball aid only, **never the actual security
  check**, which is `confirm_device`'s own constant-time verification-code
  comparison) and its report time -- **never the verification code itself**
  (work order's explicit instruction). Each row's own inline form: apartment
  select (retired apartments excluded, a UI convenience -- `confirm_device`
  already refuses one anyway, offering it would only ever produce a failing
  submission), verification-code input, `reason`, a `replace_previous`
  checkbox, and a `previous_device_target_state` select (`in_storage`/
  `faulty`).
- **`POST /ui/inventory/devices/{id}/confirm`** -- applies one device's
  confirmation. Every rule (wrong code, retired apartment, "replace_previous"
  required, device already assigned elsewhere) is enforced by
  `Storage.confirm_device` itself; this route only turns its `ValueError`
  into a re-rendered 400 with the message, plus its own empty-code/empty-
  reason/over-length-reason checks before ever calling `Storage`, the same
  pattern every other form in `fleet/ui_routes.py` already follows.

**Route naming deviates from `docs/implementation_plan.md`'s original
sketch, noted there and here.** The plan first sketched
`/ui/inventory/apartments/{id}/confirm-device`; the actual work order asked
for a single listing of every `reported` device with one confirmation form
each (a landlord does not necessarily know in advance which apartment a
freshly reported device belongs to -- that is exactly what the form asks
them to choose), which fits this package's own `/ui/inventory/devices/...`
path (parallel to `.../devices/{id}/prepare`), not a per-apartment one.

**Tests.** `tests/test_device_registration.py` (new, 35 tests, storage-level
only, against a real, migrated SQLite database): every `prepare_device`/
`record_device_report`/`confirm_device` rule named above, both the
non-obvious concurrency proofs, the constant-time-compare check (`hmac
.compare_digest` spied via `monkeypatch`), and one HTTP-level test
(`test_confirm_device_with_replace_previous_revoked_token_gets_403_on_a_
real_request`) that hits a real `/v1/heartbeat` with the just-revoked token.
`tests/test_ui_device_registration.py` (new, 25 tests) -- auth/CSRF on every
new route, the code-shown-once-and-never-logged proof, the env-var hint vs.
content cases, wrong code/empty reason/over-length reason/apartment-already-
assigned-without-replace all re-rendering with a 400 and a German message,
successful prepare/confirm, XSS escaping, and the security headers
(`Content-Security-Policy`, `X-Frame-Options`, `Referrer-Policy`,
`Cache-Control: no-store`). `fleet/storage.py`, `fleet/ui_inventory.py`, and
`fleet/ui_routes.py` are all at 100% coverage for this round (the handful of
narrow-race defensive rechecks noted above are the only lines excluded, each
with its own `# pragma: no cover` reason).

Verification for this round: `ruff check .`, `mypy .`, `mypy protocol fleet
agent tools` all clean; `python -m pytest -W ignore::ResourceWarning`
(553 passed, 99% coverage overall); the three concurrency tests
(`test_record_device_report_two_threads_same_code_exactly_one_wins`,
`test_confirm_device_wrong_attempts_counter_atomic_under_concurrency`,
`test_confirm_device_concurrent_confirms_of_two_devices_for_one_apartment_
exactly_one_wins`, and
`test_confirm_device_concurrent_confirms_of_the_same_device_exactly_one_
wins`) re-run 10x with no flake.

**Open points, left for later packages, not invented here:**

- **P4.2b** (device-side registration: Ed25519 key generation, the
  `/v1/registration/...` endpoint group, the signed-challenge exchange that
  actually issues a token and fills `DeviceRecord.public_key_fingerprint`/
  `DeviceRegistrationRecord.token_issued_at`) is not built -- `Storage
  .record_device_report` exists and is fully tested, but nothing calls it
  yet except this package's own tests. P4.2b's own work order should read
  this section before starting -- see also this file's "Cross-review
  integration: P4.2 x P4.3" section above for the three points made
  explicit for it (rate limiting, verification-code entropy, and the
  confirmed-and-bound-key precondition for token issuance).
- **P4.3** (replace device, change state) was built in parallel on its own
  branch and merged separately (`docs/implementation_plan.md`) -- see that
  package's own STATUS.md section below, and the "Cross-review
  integration: P4.2 x P4.3" section above for how `change_device_state`'s
  manual transitions now interact with this package's registration state
  (invalidating it on the relevant transitions).
- **No "resend"/"view again" for a prepared code that was lost before
  reaching the device.** The only recovery path is re-preparing the device
  (which invalidates the lost code and its own boot-partition write) --
  matches section 4's "the code expires after first use or after 24 hours"
  read literally: a lost-but-still-valid code is not a state this package
  builds a recovery UI for, since the code itself was never persisted
  anywhere to recover.
## Remove/replace device, change device state (P4.3, section 20.1-20.3)

**Decisions by the project owner, 2026-09-26 (see this package's own work
order -- do not re-open):** landlord actions stay `/ui` forms behind the
P3.0 login with CSRF, `/v1` stays agent-only; P4.2 (prepare + confirm with
verification code) is built in parallel by another agent and owns migration
`0007` -- **this package adds no migration**; the new device of a swap
always goes through P4.2's prepare/confirm flow ("no release without a
confirmed verification code", 20.3), so this package never assigns a
device to an apartment -- only a link to P4.2's "Vorbereiten" route.

**Two new pieces of business logic, both additive, neither touching
`protocol/`, `fleet/auth.py`, or `CommandType`:**

1. **The manual `DeviceLifecycle` transition table (`fleet/device_lifecycle.py`,
   new module)** -- section 20 gives no explicit transition table; this is
   a **derived reading**, stated here so it does not stay an unstated
   assumption. **Superseded by the extended, eleven-pair table** this
   package's own original five grew into -- see this file's "Cross-review
   integration: P4.2 x P4.3" section (above, once both packages were
   merged together) for the six additional pairs and why they were added;
   the original five, as this package first built them, were:

   | From | To | Reasoning |
   |---|---|---|
   | `faulty` | `in_storage` | after inspection, found reusable (20.2 step 2's "moves to faulty or in_storage") |
   | `in_storage` | `decommissioned` | permanently retiring a shelf device |
   | `registered` | `decommissioned` | retiring before it is ever placed anywhere |
   | `prepared` | `decommissioned` | retiring after "prepare" but before a device ever registers |
   | `faulty` | `decommissioned` | retiring a broken device outright, no reuse |

   Every other one of the 49 possible `(current, target)` pairs is
   **refused** (38 of them, after the extension above; 44 in this
   package's own original five-pair table), in particular:
   - `in_service -> *` is not in the table **at all** -- the only way out
     of `in_service` is `Storage.remove_device` (below), which closes the
     assignment, revokes the token, and sets the new state together,
     atomically. Allowing it here would let a landlord flip a device's
     state out from under an apartment whose assignment table still says
     that device is deployed.
   - `decommissioned -> *` is refused unconditionally (**terminal**,
     20.1's own table: "permanently out of circulation, token revoked").
   - `* -> prepared`/`* -> reported`/`* -> in_service` are never manual --
     P4.2/P4.2b's flows are the only paths into those three values.
   - `in_storage -> prepared` (reusing a shelf device for a *different*
     apartment) is **explicitly not built here** -- the work package's own
     instruction: "belongs to P4.2 (it demands the 'reset' confirmation)".

   `fleet.device_lifecycle.validate_manual_device_transition` is the
   **one place** this table is checked -- `Storage.change_device_state`
   calls straight into it (defence in depth: the check is enforced at the
   storage layer, not only trusted from the UI form's own restricted
   drop-down), and the UI's per-device state-change form
   (`fleet.device_lifecycle.allowed_manual_target_states`) only ever
   offers the targets that table would accept, so a landlord never sees an
   option the backend would then refuse. Exhaustively unit-tested over all
   7 x 7 = 49 pairs: `tests/test_device_lifecycle.py
   ::test_exhaustive_7x7_transition_table` (parametrized), plus targeted
   tests for the terminal/never-manual/`in_service`-exclusion properties
   above.

2. **"Gerät ausbauen / tauschen" (`Storage.remove_device`, section 20.2
   device-swap steps 1-2)** -- one atomic transaction:
   - closes the apartment's currently open assignment (`ended_at` = now,
     `reason` = the given reason -- overwriting the assignment's own
     creation-time reason, read as the work package's "close ... with
     until = now + reason" meaning the *closing* reason, not the original
     commissioning one);
   - sets the removed device's state to `faulty` or `in_storage`
     (`fleet.device_lifecycle.REMOVE_DEVICE_TARGET_STATES` -- the only two
     choices this action offers, section 20.2: "the old one moves to
     faulty or in_storage");
   - **revokes the apartment's agent token** (`token_hash = NULL` --
     section 20.2: "revokes the old device's token", 15.5);
   - writes **three** audit-log rows, one per kind of change (section
     20.3: "every change to assignment, state, or token is logged" -- read
     as three kinds of change, three rows, not one folded row).

   **Safe under a concurrent double removal, proven under real threads,
   not only argued** (`tests/test_storage.py
   ::test_remove_device_concurrent_double_removal_only_one_wins`, run 10x
   during verification with no flake): the assignment close is one atomic
   `UPDATE ... WHERE id = <this row> AND ended_at IS NULL`, the same
   pattern `_insert_heartbeats_ignoring_conflicts`
   (P2.1b)/`reserve_ip_login_attempt` (P3.0) already established for this
   class of race, not a read-then-write. The losing call's `UPDATE`
   affects zero rows and raises `ValueError` before writing anything else
   -- exactly one winner, no double state change, no double token
   revocation, no double audit row.

   **Rolled back atomically on any failure, proven by forcing one**
   (`tests/test_storage.py
   ::test_remove_device_rolls_back_everything_if_a_step_fails`,
   monkeypatches `Storage._write_inventory_audit_log` to raise on its
   third call within the transaction): the assignment is still open, the
   device is still `in_service`, and the token is still present
   afterward -- `Storage.session()`'s existing rollback-on-exception
   context manager (P1.3) is what makes this true, not new code in
   `remove_device` itself.

   **After revocation, a real agent request with the old token gets 403,
   proven at the HTTP level against a real `POST /v1/heartbeat`**
   (`tests/test_ui_inventory.py
   ::test_replace_device_submit_old_token_gets_403_on_a_real_heartbeat`) --
   `fleet/auth.py` is untouched (per the work package); the same
   `require_apartment_token_by_hash` -> `WHERE token_hash == <hash>`
   lookup P4.1 already proved correct for a `NULL` column value applies
   unchanged here.

**UI (`fleet/ui_routes.py`, `fleet/ui_inventory.py`, `fleet/templates/ui/
{inventory,inventory_replace_device}.html`), CSRF-checked, `Cache-Control:
no-store`, no inline style/script, German texts:**

- **`GET`/`POST /ui/inventory/apartments/{id}/replace-device`** -- the
  confirmation form: target state of the removed device, a mandatory
  reason, and the shelf (`in_storage`/`registered` devices) each linking
  to `/ui/inventory/devices/{id}/prepare` (P4.2's own route -- **only a
  link, no assignment is made here**, per the project owner's decision
  above). 404 for an unknown apartment or one with no device currently
  assigned (`fleet.ui_inventory.build_replace_device_view` returns `None`
  for both, deliberately the same response for both -- there is nothing
  to remove either way, and distinguishing them is not worth a second
  branch for what the landlord would do next regardless: pick a different
  apartment).
- **`POST /ui/inventory/devices/{id}/state`** -- the per-device
  state-change form embedded directly on `/ui/inventory` (only the manual
  transitions `fleet.device_lifecycle` allows *from that device's current
  state* are offered, so a `decommissioned`/`in_service` device renders no
  form at all, proven by `tests/test_ui_inventory.py
  ::test_inventory_view_shows_state_change_form_only_for_allowed_targets`).
  404 for an unknown device.
- A `Gerät ausbauen/tauschen` link is shown next to any apartment row that
  currently has a device assigned (`fleet.ui_inventory.ApartmentRow
  .replace_device_href`, `None` -- no link rendered -- otherwise).

**Integration note for P4.2, resolved.** This section originally read:
"built in parallel, not yet merged at the time this package was written --
decommissioning must also invalidate any pending registration ... not built
here ... the main session should add this once both branches are merged
together." **Done** -- see this file's "Cross-review integration: P4.2 x
P4.3" section above: `Storage.change_device_state` now calls `Storage
._invalidate_active_registration` whenever a transition leaves `prepared`/
`reported` or lands on `decommissioned`, in the same transaction, with its
own audit row.

**Invariant checked directly, not just assumed: a `decommissioned` device
has no open assignment, so there is no token to revoke via this path.**
`Storage.change_device_state` never touches `ApartmentRecord.token_hash`
at all (only `Storage.remove_device` does, and only for the apartment
whose *open* assignment it closes) -- a device reaching `decommissioned`
through this form was, per the transition table above, already
`registered`/`prepared`/`in_storage`/`faulty` beforehand, none of which
carry an open assignment (only `in_service` does, and `in_service` is
never a source in this table) -- there is structurally nothing for this
path to revoke. `test_change_device_state_decommissioned_is_terminal`
(and every other `change_device_state` test) never touches
`ApartmentRecord` at all, which is itself the proof: nothing in that
method's code path reaches an apartment row.

**Tests.** `tests/test_device_lifecycle.py` (new, 59 tests): the
exhaustive 7x7 transition table, the terminal/`in_service`/never-into
properties above, `allowed_manual_target_states`. `tests/test_storage.py`
gained 16 new tests (`change_device_state`: allowed transition, exactly
one audit row, unknown device, empty reason, disallowed transition writes
nothing, `in_service` refused, `decommissioned` terminal;
`remove_device`: success to both target states, three audit rows, unknown
target state, empty reason, unknown apartment, no open assignment, the
forced-rollback test, the concurrent-double-removal test run 10x during
verification). `tests/test_ui_inventory.py` gained 23 new tests: every new
route's auth/CSRF/404/validation-error path (including the two over-length
`reason` cases and the unknown-target-state case that reaches `Storage
.remove_device`'s own `ValueError`), the successful state change and
remove/replace flow (asserting storage side effects and audit rows), the
old-token-gets-403-on-a-real-heartbeat test, the state-change form only
appearing for devices with allowed targets, the replace-device link only
appearing for an assigned apartment, and the new template's
inline-style/script check.

Verification for this round (fresh venv, `python3.13 -m venv`,
`pip install -e ".[dev,fleet,agent]"` -- SQLAlchemy 2.1.1, mypy 2.3.1):
`ruff check .`, `mypy .`, `mypy protocol fleet agent tools` all clean;
`python -m pytest -W ignore::ResourceWarning` (591 passed, 99% coverage
overall, every P4.3 file -- `fleet/device_lifecycle.py`, `fleet/storage.py`,
`fleet/ui_inventory.py`, `fleet/ui_routes.py` -- at 100%); both concurrency
tests (`test_remove_device_concurrent_double_removal_only_one_wins`, plus
P4.1's own `test_partial_unique_index_for_assignments_is_safe_under_
concurrent_calls`) re-run 10x in a row, no flake.

## Inventory foundation + "Inventar" view (P4.1, section 20, absorbs P3.3)

**Decisions by the project owner, 2026-09-26 (do not re-open, see this
package's own work order):**

1. **Landlord inventory actions are server-rendered `/ui` forms**, behind
   the P3.0 login (`require_ui_user`) with the per-session CSRF token on
   every POST -- **not** `/v1` endpoints. `/v1` stays a pure agent API
   (section 4/18.1's bearer-token check). The six `/v1` inventory stubs
   that used to sit in `fleet/app.py` (`read_inventory`, `register_device`,
   `prepare_device`, `confirm_device_registration`, `replace_device`,
   `change_device_state`, plus `DeviceReplacementRequest`/
   `DeviceStateRequest`) are **removed**, together with their tests in
   `tests/test_fleet.py` -- replaced by a single parametrized test
   (`test_old_v1_inventory_routes_no_longer_exist`) confirming all six
   paths now 404. P4.2/P4.3 add the remaining device-lifecycle actions
   (prepare/confirm/replace/state) the same way, under `/ui/inventory/...`,
   never `/v1/...`.
2. **P3.3 ("Inventory" view) is built here, not as its own package** --
   `docs/implementation_plan.md`'s P3.3 entry is marked done and points
   here; building the schema and the one view that reads it as two
   separate packages would have made the second only read what the first
   just wrote, with nothing else to develop against in between.

**Schema (`fleet/migrations/versions/0006_inventory.py`, `down_revision`
`"0005"`).** Four new/extended entities, section 20.1:

- **`properties`** (`id` autoincrement -- the specification gives a
  property no permanent id of its own the way an apartment has one --
  `name`, `address`, `notes`).
- **`apartments`** extended with `property_id` (FK to `properties.id`,
  **nullable** -- a legacy row has none, see below), `label`, `floor`,
  `orientation`, `state` (`ApartmentState` value), `heating_circuits`,
  `pilot_mode`. **`token_hash` becomes nullable** (the work package's
  explicit instruction: "an apartment exists before any device is
  confirmed") -- the existing unique index is untouched, since SQL's own
  "NULL is never equal to another NULL" semantics already let a unique
  index hold any number of `NULL` rows without a special case (verified
  directly, not just assumed: `tests/test_storage.py
  ::test_unique_index_on_token_hash_still_permits_multiple_null_rows`).
- **`devices`** (`id` is the serial/hardware id itself, a natural key like
  `apartments.id` -- `model`, `acquisition_date`, `public_key_fingerprint`
  nullable "until registration", `image_version`, `watchdog_version`,
  `state`, a `DeviceLifecycle` value).
- **`assignments`** (`device_id`, `apartment_id`, `started_at`, `ended_at`
  nullable, `reason`) -- named `started_at`/`ended_at` at the column
  level, not `from`/`until` verbatim (a reserved word in more than one SQL
  dialect). **Two partial unique indexes enforce section 20.3's first two
  rules at the database level**, the same pattern
  `0004_alarms.py`'s partial unique index already established for exactly
  this class of race: `ux_assignments_apartment_id_open` (at most one row
  with `ended_at IS NULL` per apartment -- "an apartment has at most one
  active device") and `ux_assignments_device_id_open` (same, per device --
  "a device belongs to at most one apartment"). Both proven under real
  concurrent threads against a real, migrated SQLite database, not only
  single-threaded (`tests/test_storage.py
  ::test_partial_unique_index_for_assignments_is_safe_under_concurrent_calls`,
  5 threads racing to assign 5 different devices to one apartment --
  exactly one wins, the other four get a `ValueError`, exactly one open
  row remains).
- **`inventory_audit_log`** (`timestamp`, `ui_username`, `entity_type`,
  `entity_id`, `action`, `reason` nullable, `before_json`/`after_json` --
  short JSON snapshots of only the *changed* fields, never a full-row
  dump, so this table cannot itself become a second place section-6 data
  could leak from). Written **in the same transaction as the change it
  describes** (`Storage._write_inventory_audit_log` takes the caller's
  already-open session, never opens its own) -- section 20.3: "every
  change to assignment, state, or token is logged: who, when, why".

**Existing apartment rows (P1.1-P3.x, id + token_hash only) migrate with
these defaults, applied by `0006_inventory.py`'s own data backfill, not
left for the application layer to paper over on first read:**

| Column | Default for a legacy row | Why |
|---|---|---|
| `property_id` | `NULL` | No property existed before this package -- nothing to backfill it from. |
| `label` | the apartment's own `id` | `protocol.inventory.Apartment.label` is required (`min_length=1`); the id is the only value already known for every existing row. |
| `floor`/`orientation` | `NULL` | Both optional in the protocol model. |
| `state` | `"occupied"` | Every apartment that reached P1.1-P3.x already has a real agent token and is understood to be a live, in-service apartment -- the least surprising default for "was already running", not a claim about actual tenancy. |
| `heating_circuits` | `0` | The only value the previous schema carries no information to derive at all -- corrected via the "edit apartment" form. |
| `pilot_mode` | `false` | `Apartment.pilot_mode`'s own default (section 21.4), applied identically to a migrated row. |

Proven with a real pre-migration row, not only asserted:
`tests/test_storage.py::test_migration_0006_backfills_a_legacy_apartment_row`
inserts a bare `(id, token_hash)` row against a database migrated only to
`0005`, then upgrades to `0006` and asserts every default above.
`test_migrations_match_the_orm_model_exactly` (the repository's existing
`compare_metadata` guard) stays green -- `ApartmentRecord`'s new mapped
columns match this migration's schema exactly, including the `NOT NULL`
constraints `label`/`state`/`heating_circuits` end up with once backfilled
(added via a second `batch_alter_table` pass, after the data backfill, not
before it -- SQLite would otherwise reject the `NOT NULL` on existing rows
mid-backfill).

**`Storage.set_apartment_token` fills in the same defaults** when it
creates a brand-new apartment row (the shape every P1.1-P3.x test still
uses: registering only a token, never going through the inventory UI) --
`label` = the apartment id, `state` = `"occupied"`, `heating_circuits` =
`0`, `pilot_mode` = `False`, no property. An apartment created *through*
`Storage.create_apartment` first is unaffected; this only ever fills in a
row that did not exist yet.

**The NULL-token-hash rule, proven, not just argued.** `fleet/auth.py` is
**untouched** by this package (the work package's explicit instruction) --
both dependencies already behave correctly for a `NULL` `token_hash`
without any code change:

- `require_apartment_token` (apartment from the address, e.g.
  `POST /v1/events/{apartment}`): `stored_hash is None or not
  hmac.compare_digest(...)` already short-circuits on `None` before ever
  calling `compare_digest` -- a `NULL` column value and "no apartment row
  at all" produce the exact same `stored_hash is None` branch.
- `require_apartment_token_by_hash` (apartment resolved by hash, e.g.
  `POST /v1/heartbeat`): `WHERE token_hash == <hash>` is a SQL comparison
  a `NULL` column value can never satisfy, for any presented hash
  including the hash of an empty string (checked explicitly).

Four new HTTP-level tests in `tests/test_fleet.py`
(`test_null_token_hash_apartment_gets_403_on_{heartbeat,event,
commands_stream,command_result}`) create an apartment via
`Storage.create_apartment` (token-less, exactly the P4.1 "exists before
confirmation" case) and confirm 403 on all four token-checked endpoints,
plus two direct storage-level tests
(`test_get_apartment_token_hash_returns_none_for_a_null_hash`,
`test_get_apartment_id_by_token_hash_never_matches_a_null_hash`).

**A second, real interaction this package's own change surfaced, fixed in
the same package (not left as a regression for a later one to find):**
P3.2's `fleet/ui_apartment.py::build_apartment_detail` used
`Storage.get_apartment_token_hash(id) is None` as its "does this apartment
exist" check -- correct before this package (every apartment had exactly
one token-hash row), wrong after it (an inventory-created apartment with
no device yet now legitimately has a `NULL` token but very much exists,
and would have 404'd on "Eine Wohnung" until a device was confirmed to
it). Fixed by adding `Storage.get_apartment_label` (returns the row's
`label`, or `None` for a genuinely unknown apartment -- `label` is
`NOT NULL` for every row that exists, so `None` is unambiguous) and
switching `build_apartment_detail`'s existence check to that, which
doubles as the label to show alongside the id (see below).
**Corrected (cross-review, 2026-09-26):** this section previously claimed
the fix was "covered going forward by every existing P3.2 test" -- false,
`tests/test_ui_apartment.py` still only ever calls
`Storage.set_apartment_token` (which always creates a token), so no
existing test actually exercises a token-less, `create_apartment`-created
apartment through this route at all. Fixed with direct tests instead:
`tests/test_storage.py
::test_get_apartment_label_returns_the_label_for_a_token_less_apartment`
(storage level: `get_apartment_token_hash` is `None`, `get_apartment_label`
still resolves) and `tests/test_ui_apartment.py
::test_apartment_created_via_inventory_without_a_token_is_200_not_404`
(HTTP level: a `create_apartment`-created apartment renders 200, not 404,
on `GET /ui/apartments/{id}`) -- both new, both actually exercise the
token-less path this fix was for. `Storage.get_apartment_label` itself
also gained its own direct unit tests
(`test_get_apartment_label_returns_the_label_for_a_known_apartment`,
`::test_get_apartment_label_returns_none_for_an_unknown_apartment`), not
only incidental coverage via `build_apartment_detail`.

**Apartment label shown alongside the id, "if cheap" (work package's own
instruction).** `Storage.get_house_overview` now also selects `label` in
its existing per-apartment query (no extra query); `fleet.ui_house
.ApartmentTile`/`fleet.ui_apartment.ApartmentDetail` both gained a `label`
field, and `index.html`/`apartment.html` show it in parentheses next to
the id when it differs from the id itself (a legacy row's label defaults
to its id, so showing it twice would be noise, not information).

**Storage methods (`fleet/storage.py`, new "-- inventory (P4.1, section
20) --" section), all unit-tested directly against a real, migrated
SQLite database:** `create_property`/`list_properties`/`get_property`;
`create_apartment` (raises `ValueError` on a duplicate id, not an
uncaught `IntegrityError`)/`get_apartment`/`list_apartments`/
`list_apartments_by_property`; `update_apartment` (the single "edit
apartment" form's backend -- label/floor/orientation/heating_circuits/
state/`pilot_mode` all in one call, **always** requires a non-empty
`reason`, writes exactly one audit row per call that actually changes
something, none for a resubmitted, unchanged form);
`register_device` (no `state` parameter at all -- "a caller must not be
able to register a device in any state other than `registered`" is
therefore structurally impossible to violate, not merely validated
away)/`get_device`/`list_devices`; `get_current_assignment`/
`get_current_device_for_apartment` (two queries, not a join, mirroring
`get_house_overview`'s existing "acceptable for a handful of apartments"
reasoning); `create_assignment` (not used by any P4.1 route -- P4.2's
"confirm device registration" flow will call it -- provided here so the
two partial unique indexes have a tested entry point, including under
concurrency, see above); `list_audit_log_for_entity`.

**UI (`fleet/ui_inventory.py`, `fleet/ui_routes.py`, `fleet/templates/ui/
{inventory,inventory_apartment_edit}.html`, `fleet/static/ui/
fleet-ui.css`), all behind `require_ui_user`, CSRF-checked on every POST,
`Cache-Control: no-store`, no inline style/script:**

- `GET /ui/inventory` -- properties (each with its apartments, each
  apartment with its current device via its open assignment if any),
  apartments with no property (a legacy row), and every device not
  `in_service`, optionally narrowed by `?filter=in_storage` or
  `?filter=faulty` (section 20.4's own two named filters -- any other
  value is silently treated as "no filter", the same forgiving handling
  `fleet.ui_apartment.clamp_history_days` already applies to its own query
  parameter). "Inventar" nav link added to `base.html`.
- `POST /ui/inventory/properties` -- create a property. No `reason`/audit
  log: a brand-new property changes no prior assignment, state, or token.
- `POST /ui/inventory/apartments` -- create an apartment. The id is
  validated against `APARTMENT_ID_PATTERN` (matching the specification's
  own example `house7-a03`: lowercase letters, digits, and `-` only
  between two alphanumeric characters -- **cross-review, 2026-09-26:** a
  leading or trailing `-` is rejected too, not just disallowed characters)
  here, in the UI layer, kept separate from `Storage.create_apartment`'s
  own uniqueness guarantee (the primary key) -- **there is no field, on
  this form or any other, that could ever change the id afterward.**
  `state` always starts at
  `occupied` and `pilot_mode` always at `False` regardless of anything a
  form could otherwise carry (section 21.4: "a newly created apartment is
  never accidentally in pilot mode") -- both changeable only via the
  separate edit form. No `reason`/audit log, same reasoning as properties.
- `POST /ui/inventory/devices` -- register a device. **No `state` field on
  this form at all** -- `Storage.register_device` has no `state`
  parameter, so "ends up `registered` regardless of what is submitted" is
  true by construction, not by a check that could be bypassed or
  forgotten. No `reason`/audit log, same reasoning as the two forms above.
- `GET`/`POST /ui/inventory/apartments/{id}/edit` -- edit label, floor,
  orientation, heating circuits, state, `pilot_mode`. **A non-empty
  `reason` is mandatory on every submission** (not only when
  `state`/`pilot_mode` actually changes) -- the simplest rule that
  satisfies both section 20.3's "every change to ... state ... is logged"
  and CLAUDE.md principle 5's "pilot_mode changes are security-relevant,
  log with a mandatory reason" without special-casing which of the six
  fields actually changed. `state` includes `retired` as an ordinary
  choice -- section 20.3: "an apartment is not deleted, it is retired" --
  there is no delete action anywhere in this module, proven by
  `tests/test_ui_inventory.py
  ::test_apartment_edit_submit_retire_apartment_does_not_delete_it` (the
  row still exists, still 200s on `GET`, and is still listed on the
  inventory page afterward). `pilot_mode` is an ordinary HTML checkbox
  (present = checked = `True`, absent = unchecked = `False`).

**Section 6/20.1 stays out.** No tenant name or contact detail is read,
shown, or collected anywhere in this package -- `protocol.inventory
.Property`/`Apartment` already exclude the category (unchanged, `protocol/`
was not touched); the "create property" form additionally shows a static
German hint ("keine Mieterdaten") next to the free-text `notes` field,
since a free-text column cannot structurally enforce what a landlord
chooses to type into it the way a typed field can. Verified directly by
`tests/test_ui_inventory.py
::test_xss_escaping_of_property_and_apartment_free_text_fields`, which
also confirms every free-text field (property name/address/notes,
apartment label/floor/orientation) is HTML-escaped, not merely absent of
section-6 markers.

**Cross-review round 1 (2026-09-26) -- four fixes, all landed here, not
deferred:**

1. **`POST /v1/heartbeats` (the P2.1b catch-up batch endpoint) was missing
   from the NULL-token-hash test group** -- the other three token-checked
   endpoints (`POST /v1/heartbeat`, `POST /v1/events/{apartment}`,
   `GET /v1/commands`, `POST /v1/commands/{id}/result`) each had one, this
   one did not, purely an oversight, not a code gap (the same
   `require_apartment_token_by_hash` dependency already covers it). Added:
   `tests/test_fleet.py::test_null_token_hash_apartment_gets_403_on_heartbeats_batch`.

2. **Downgrading past 0006 with a token-less apartment left a permanent,
   orphaned `_alembic_tmp_apartments` table behind, on top of a raw,
   unhelpful `IntegrityError`.** Reproduced: `token_hash` becoming `NOT
   NULL` again requires every existing row to already have one; SQLite's
   `batch_alter_table` implements this as "build a new table, copy every
   row across, swap it in" -- the copy step is what actually violates the
   new constraint, but only *after* the new table already exists, and
   nothing in the failed migration ever gets to drop it again. **Decision
   (main session): do not backfill a placeholder token to paper over
   this** -- a synthetic, made-up hash written by a migration is exactly
   the kind of artificial secret-shaped value CLAUDE.md's "no secrets in
   the repo, not even as a real-looking example value" reasoning was meant
   to rule out, database write or source-code literal alike. **Fixed with
   an explicit pre-check**: `downgrade()` now queries for any apartment
   with `token_hash IS NULL` *before* touching the schema at all, and
   raises `RuntimeError` naming exactly which apartment id(s) block it --
   nothing is created, nothing is touched, if any are found. Proven, not
   just argued: `tests/test_storage.py
   ::test_migration_0006_downgrade_refuses_when_an_apartment_has_no_token`
   asserts the message, that no `_alembic_tmp_apartments` table exists
   afterward, and that the schema and the apartment's own data are both
   still intact at revision `0006`.

3. **No length bounds on form input, anywhere.** On SQLite an over-length
   `VARCHAR` is silently truncated; on PostgreSQL it raises -- a
   deployment that switches engines would discover the difference as a
   500 in production, not as a validation message. **Fixed:**
   `fleet/ui_inventory.py` now defines one `MAX_*_LENGTH` constant per
   column (mirroring `0006_inventory.py`'s own column definitions exactly,
   restated since a migration module is not something this module
   imports from), and every free-text field on every form (property
   name/address; apartment id/label/floor/orientation; device id/model/
   image version/watchdog version; the edit form's `reason`) is checked
   against it before `Storage` ever sees the value, re-rendering with the
   same graceful 400 message every other validation error already gets --
   a new shared helper, `fleet.ui_routes._first_length_error`, checks a
   list of `(field name, value, max length)` triples and returns the first
   violation, or `None`. Tested: an over-length apartment id (129 chars,
   past the column's 128), an over-length property name (256, past 255),
   apartment label, device model, and the edit form's reason (501, past
   500) each rejected with a 400 and nothing written --
   `tests/test_ui_inventory.py
   ::test_create_apartment_rejects_an_over_length_id` and five sibling
   tests.

**Also fixed, optional items from the same review:**

- **A leading or trailing `-` in an apartment id is now rejected**
  (`APARTMENT_ID_PATTERN` changed from `^[a-z0-9-]+$` to
  `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`) -- `house7-a03-`/`-house7-a03` are
  easy mistypes no legitimate id needs, and internal `-` (the actual
  specification example, `house7-a03`) is still fully allowed. Tested at
  both the pattern level (`tests/test_ui_inventory.py
  ::test_apartment_id_pattern_rejects_a_leading_or_trailing_hyphen`) and
  the HTTP level (`::test_create_apartment_rejects_a_leading_hyphen_in_the_id`).
- **XSS escaping test added for the device `model` field**
  (`tests/test_ui_inventory.py::test_xss_escaping_of_device_model_field`)
  -- the existing XSS test only covered property/apartment free-text
  fields, not a device's.
- **A direct `sqlite_master` assertion for both partial unique indexes'
  `WHERE` clause** (`tests/test_storage.py
  ::test_migration_0006_partial_unique_indexes_carry_the_where_clause`) --
  the existing concurrency test proves the indexes *behave* as partial
  (a closed assignment does not block a new one), this additionally
  proves the stored schema itself says `WHERE ended_at IS NULL`, not just
  that the test data used elsewhere never happened to hit the
  non-partial case.

**Tests.** `tests/test_storage.py` gained 31 new tests (87 total: migration
up/down/backfill, the refused-downgrade case and the `sqlite_master`
partial-index check above, every new `Storage` method including
`get_apartment_label`'s own direct tests, both partial unique indexes
including the concurrency test); `tests/test_ui_inventory.py` (new, 52
tests) covers `fleet.ui_inventory.build_inventory_view` directly
(grouping, current-device lookup, both named filters, an unknown filter
falling back to "no filter", a retired apartment still listed) and every
route at the HTTP level, logging in via the real P3.0 flow: unauthenticated
access redirecting (303) for the view and every POST; missing/wrong CSRF
(403); every validation error (empty label, non-numeric/unknown property
id, negative heating circuits, malformed date, duplicate ids, missing
reason, unknown state, a leading/trailing hyphen, every field's length
bound) re-rendering with a German message (400), not a 500 or a silent
no-op; success writing the entity and, for the edit form, an audit entry
with the acting username and reason; the `in_storage`/`faulty` filters;
XSS escaping (property/apartment free-text fields and the device `model`
field); the security headers. `tests/test_fleet.py` (72 total) replaced
its six removed-endpoint tests with `test_old_v1_inventory_routes_no_
longer_exist` (parametrized over all six paths, asserts 404) and gained
seven NULL-token-hash tests: one per token-checked endpoint (`POST
/v1/heartbeat`, `POST /v1/heartbeats`, `POST /v1/events/{apartment}`,
`GET /v1/commands`, `POST /v1/commands/{id}/result` -- five, including
the batch endpoint added in cross-review round 1 below) plus two direct
storage-level tests. `tests/test_ui_apartment.py`
(43 total) gained
`test_apartment_created_via_inventory_without_a_token_is_200_not_404`.

Verification for this round: `ruff check .`, `mypy .`, `mypy protocol
fleet agent tools` all clean; `python -m pytest -W ignore::ResourceWarning`
(493 passed, 99% coverage overall, every P4.1 file --
`fleet/storage.py`, `fleet/ui_inventory.py`, `fleet/ui_routes.py`,
`fleet/app.py`, every migration -- at 100%).

**Cross-review round 2 (2026-09-26) -- one gate failure, environment-
dependent, fixed.** `fleet/migrations/versions/0006_inventory.py::downgrade`'s
`tokenless_ids = connection.execute(...).scalars().all()` (added in round
1's fix, see above) type-checked cleanly in the venv it was written in
(SQLAlchemy 2.0.54) but failed `mypy .`/`mypy protocol fleet agent tools`
in a fresh venv built against SQLAlchemy 2.1.1 (both mypy 2.3.1):
`error: Need type annotation for "tokenless_ids" [var-annotated]` -- a
newer `CursorResult.scalars().all()` return-type stub apparently no
longer lets mypy infer the assignment target's type on its own. **Fixed
with an explicit annotation**, `tokenless_ids: Sequence[str] = (...)`
(`Sequence` was already imported in this module for the revision-id type
hints) -- reproduced the failure first (temporarily reverting to the
unannotated form against the same fresh venv, confirmed the exact error),
then confirmed the fix resolves it. Re-verified in **two** environments
this round, not just the one this package was developed in: this
worktree's existing venv (Python 3.14.6, SQLAlchemy 2.0.54, mypy 2.3.1)
and a genuinely fresh venv built the same way a reviewer would
(`python3.13 -m venv ...`, `pip install -e ".[dev,fleet,agent]"` --
Python 3.13.14, SQLAlchemy 2.1.1, mypy 2.3.1) -- both clean.

**Open points, left for later packages, not invented here:**

- **P4.2/P4.2b/P4.3** (prepare/confirm/replace/state, device-side Ed25519
  registration) are not built -- `Storage.create_assignment` exists and is
  tested, but no `/ui` route calls it yet.
- **No bulk "assign an existing (legacy) apartment to a property" tool.**
  A legacy apartment's `property_id` stays `NULL` until someone edits it
  through a future package -- P4.1's own "edit apartment" form does not
  offer changing `property_id` either (not asked for by the work package;
  the create-apartment form is the only place a property is chosen).
- **`inventory_audit_log` has no UI page of its own yet** -- read only via
  `Storage.list_audit_log_for_entity` (used by tests), not surfaced
  anywhere in the "Inventar" view. A future package could add a per-
  apartment/per-device history section.

## "Eine Wohnung" -- the fleet UI's apartment detail view (P3.2, section 9's second view)

`GET /ui/apartments/{apartment_id}` (behind `require_ui_user`, same as P3.1)
renders section 9's second view: heartbeat history, open/past faults,
battery/signal aggregates, version, system/control state, and
open/recent alarms for one apartment -- everything except stage-1/2
commands, which are explicitly deferred to a later step. Sits directly on
top of P3.1 (`fleet/ui_house.py`'s pattern is repeated, not reinvented) and
P1.2/P2.1/P2.2 (events, heartbeats, alarms) -- no new migration, no change
to `protocol/`, `fleet/auth.py`, `fleet/ui_auth.py`, or `CommandType`.

**Where the pieces live.** `fleet/storage.py` gained `HeartbeatHistoryEntry`
(a `sent_at`/`received_at` pair) and three new bounded `Storage` read
methods: `get_heartbeat_history(apartment_id, since)` (ascending by
`sent_at`, capped at `_MAX_HEARTBEAT_HISTORY_ROWS = 20_000` -- comfortably
above the 14-day cap's worst case of 10,080 rows at one heartbeat per
120 s), `list_events_for_apartment(apartment_id, since)` (newest first,
capped at `_MAX_EVENT_HISTORY_ROWS = 200`), and
`list_alarms_for_apartment(apartment_id)` (newest first, capped at
`_MAX_APARTMENT_ALARM_ROWS = 50`, not date-windowed -- an apartment
realistically accumulates far fewer alarm rows than heartbeats). Apartment
existence (for the 404 case) is checked by reusing
`Storage.get_apartment_token_hash` -- every registered apartment has
exactly one token-hash row (`Storage.set_apartment_token`), so no separate
"does this apartment exist" method was needed.

`fleet/ui_apartment.py` (new) is the view-model module, the same split
P3.1 established: `build_apartment_detail(storage, apartment_id, now,
days)` returns `None` for an unknown apartment (`fleet/ui_routes.py` turns
that into a 404, same layout, no data) or an `ApartmentDetail` with every
field already derived and German-rendered -- the template
(`fleet/templates/ui/apartment.html`) only iterates and prints, exactly
like `index.html`.

**`days` is `str | None` at the route, not `int | None` (cross-review
round 1 fix).** The first version of this package typed
`apartment_detail`'s `days` query parameter as `int`, relying on
FastAPI/Pydantic's own coercion plus `clamp_history_days` to handle
out-of-range values -- cross-review found this did not actually deliver
"never a 422" as the docstring/this file claimed: an `int`-typed query
parameter makes FastAPI reject `?days=abc`, `?days=3.5`, and
`?days=1e400` with a 422 *before* the route body (and therefore
`clamp_history_days`) ever runs, for exactly the malformed-input case the
work package asked to be tolerant of. Fixed by typing `days: str | None`
at the route and moving all parsing into `clamp_history_days` itself,
which now accepts `int | str | None`: a `str` that does not parse as a
plain base-10 integer (`int(days)`, which itself already rejects
`"3.5"`/`"1e400"`/`"abc"` with `ValueError`) degrades to
`DEFAULT_HISTORY_DAYS` (3), the same fallback an out-of-range value
already got. Regression tests, all at the HTTP level:
`tests/test_ui_apartment.py::test_days_query_parameter_non_numeric_is_not_a_422`,
`::test_days_query_parameter_a_float_string_is_not_a_422`,
`::test_days_query_parameter_scientific_notation_is_not_a_422` (each posts
one of the three inputs cross-review named, asserts 200 with the default
3-day heading, not 422).

**Gap detection (section 5) -- the open point this package closes.**
P2.1b's `docs/STATUS.md` entry explicitly deferred "gap detection for
caught-up heartbeats ... displaying it is a UI concern that belongs with
P3.2" -- closed here. `fleet.ui_apartment._build_timeline` treats any
interval between two *consecutive* stored heartbeats (ordered by
`sent_at`, matching section 5's own wording, not `received_at`) strictly
longer than `fleet.alarms.ABSENCE_THRESHOLD` (six minutes) as a gap --
**reusing that constant directly, not a second, independently-chosen
number**, so this view's definition of "gap" can never silently drift
from P2.2's definition of "absent" (section 8's own headline alarm), per
the work package's explicit instruction. A heartbeat whose `received_at`
is itself more than `ABSENCE_THRESHOLD` after its own `sent_at` is marked
"nachgeliefert" (caught up) -- the same reused threshold, since the agent
sends a live heartbeat every 120 s, so a receipt delay past six minutes
can only happen for a heartbeat that was buffered and delivered later via
a P2.1b catch-up batch. **Window edges are a hard cut, not smoothed
over:** `Storage.get_heartbeat_history` only ever returns rows with
`sent_at >= since`; a heartbeat sent before the requested window is never
used to fabricate a gap against the first heartbeat that *is* in the
window -- `tests/test_ui_apartment.py
::test_gap_detection_ignores_data_outside_the_requested_window` builds
exactly this scenario (an old heartbeat outside a 1-day window, then a
real 22h gap fully inside it) and asserts exactly one gap, not two.

**Reachable runs are aggregated into one row each; gaps never are
(cross-review round 1, main-session finding).** The first version of this
package rendered one `<li>` per stored heartbeat -- up to ~10,000 rows for
the 14-day cap at one heartbeat every 120 s, found not to scale. Fixed:
`fleet.ui_apartment._close_run` collapses each *contiguous* run of
reachable heartbeats (no gap between any two consecutive ones) into a
single `TimelineEntry` -- "Erreichbar, von X bis Y, N Herzschläge" (or
just "vor X" for a run of exactly one), plus how many of that run's
heartbeats were caught up (`caught_up_count`). **A gap keeps its own row
regardless** -- `_build_timeline` still emits one `TimelineEntry` per
detected gap, with its own start/end/duration, exactly as before; only the
*reachable* rows in between are now summarised, never a gap itself,
matching cross-review's own framing: "section 5 forbids smoothing over
gaps, not summarising reachable periods." Regression tests:
`tests/test_ui_apartment.py::test_a_long_run_of_heartbeats_collapses_into_one_row`
(many heartbeats, one `TimelineEntry`, correct `heartbeat_count`),
`::test_two_runs_separated_by_a_gap_produce_run_gap_run`,
`::test_caught_up_count_is_per_run_not_global`, and the pre-existing
exactly-at-threshold/just-above-threshold tests (unchanged assertions,
now read against the aggregated entries).

**The interval from the last stored heartbeat up to `now` is deliberately
never rendered as a trailing gap row (cross-review round 2, explicit
call-out).** `_build_timeline` only ever compares consecutive *stored*
heartbeats against each other; there is no closing check of `now -
last_heartbeat.sent_at` after the loop. An apartment that has simply gone
silent and not reported since is already surfaced by the open "meldet
sich nicht" alarm in this same page's "Alarme" section
(`ApartmentDetail.alarms`, P2.2's `check_absence_alarms`) -- a second,
differently-worded row for the exact same still-ongoing silence at the
bottom of the timeline would add no information, only a second place for
the two to eventually disagree (e.g. a `days` window that excludes the
alarm's own `raised_at`, or independent wording drift between "Lücke" and
"Meldet sich nicht"). A *closed* gap between two heartbeats that both
arrived is a different, already-resolved fact about the past, and keeps
its own row exactly as before. Pinned by
`tests/test_ui_apartment.py::test_a_stale_last_heartbeat_is_not_rendered_as_a_trailing_gap`
(a last heartbeat two days old, `now` two days later -> exactly one run,
no trailing gap).

**Battery/signal values -- section 9's wording vs. the actual protocol
(open point, not built, not invented).** Section 9 says "battery and
signal values ... per device"; `protocol.heartbeat.DeviceState` only ever
carries the fleet-wide aggregates (`weakest_battery_percent`,
`worst_signal_quality`, `silent_devices`, `zigbee_bridge`) -- there is no
per-device breakdown anywhere in the heartbeat wire contract. This page
shows the aggregates only, with an explanatory note in the template ("Werte
je einzelnem Gerät werden vom Heartbeat-Protokoll nicht übertragen").
Adding per-device values would need a `protocol/` extension, which
CLAUDE.md's security principles require clearing with the project owner
first (section 18.2: "a field may only ever be added", a *deliberate*
addition, not incidental to a UI package) -- not invented here, left open.

**Commands -- explicitly deferred (work package's own instruction, section
9: "the four to seven allowed commands as buttons with confirmation").**
Not built by this package (a later step, once `POST /v1/commands/...` and
the SSE channel exist per `docs/implementation_plan.md`'s own ordering).
The page shows a short static German note in place of any button/form; no
new `POST` route exists on `fleet/ui_routes.py` for this page.

**Section 6 stays out, same as P3.1.** No room temperature, setpoint,
schedule, or tenant data is read, shown, or derivable -- `EventRecord` has
no `titel`/`text` column at all (P1.2), so past faults can only ever show
`schluessel`/`schwere`/derived kind/receipt time, structurally nothing else
to leak; verified directly by
`tests/test_ui_apartment.py::test_events_never_show_titel_or_text_even_with_no_prefix_match`,
which posts a real event through `POST /v1/events/{apartment}` with marker
`titel`/`text` values and asserts both are absent from the rendered page,
and by `::test_no_section_6_data_anywhere_on_the_page` (same forbidden-marker
list P3.1 uses: `"°C"`/`"Sollwert"`/`"Zeitplan"`/`"Mieter:"`/`"Kontakt:"`).

**Hardened apartment links (P3.1 review, closed here for both views).**
`fleet/ui_routes.py` registers a Jinja filter, `urlpath`
(`urllib.parse.quote(value, safe="")`) -- **not** Jinja's own built-in
`urlencode`, which deliberately leaves `/` unescaped (correct for a query
string, wrong for a path *segment* that may itself contain a literal `/`).
`index.html`'s tile link now reads
`/ui/apartments/{{ tile.apartment_id | urlpath }}`. On the receiving end,
`fleet/ui_routes.py::apartment_detail` uses Starlette's `{apartment_id:path}`
converter, not the plain (default) `str` one -- **confirmed empirically
before choosing it:** a `%2F` inside a URL is decoded by the ASGI
server/client before Starlette's router splits the path into segments, so
a plain `str` parameter (whose regex excludes `/`) 404s for an id that
actually contained a `/`, even though the link encoded it correctly; the
`:path` converter matches everything after the prefix and round-trips
`/`, a space, and `<` correctly, covered by
`tests/test_ui_apartment.py::test_encoded_id_link_from_the_house_view_resolves_to_this_page`
(builds a real apartment id containing all three, follows the house
view's own rendered `href`, asserts 200) and
`::test_xss_escaping_of_id_zone_mode_and_key`.

**Consistent with P3.4's own encoding, not a duplicate mechanism to
reconcile.** P3.4 (built in parallel, merged afterward, see its own
section below) independently reached `urllib.parse.quote(id, safe="")`
for its own task-list links, as a plain Python helper
(`fleet.ui_tasks._apartment_href`) rather than this package's Jinja
filter -- both call the exact same stdlib function with the exact same
arguments, so the two produce byte-identical encoded output; there is
nothing to unify beyond noting it here, and P3.4's own "pre-existing gap,
not this package's to fix" note about `index.html` linking with a raw,
unencoded id is stale now that this package's `urlpath` filter closes it
(see that section for the corrected wording).

**Route-ordering note, added at the route itself, not only here
(cross-review round 1).** `fleet/ui_routes.py::apartment_detail`'s
`{apartment_id:path}` converter greedily matches everything after
`/ui/apartments/`, including a literal suffix a future, more specific
sub-route might want (e.g. `/ui/apartments/export`) -- FastAPI/Starlette
match routes in registration order, so any such future route must be
registered *before* this one in `fleet/ui_routes.py`, or it would never be
reached. A one-line comment directly above the route's decorator states
this for whoever adds the next one; `/ui/tasks` (P3.4) is unaffected, since
it is a different top-level `/ui/...` path, not a `/ui/apartments/...`
suffix.

**Tests.** New `tests/test_ui_apartment.py` (42 tests, up from 30 after
cross-review rounds 1 and 2's fixes) against a real, migrated SQLite database, no
mocks, mirroring `tests/test_ui_house.py`'s structure: `clamp_history_days`
unit-tested directly (`None`/zero/negative -> default, over-range ->
capped, in-range passthrough, plus a valid numeric string and three
malformed strings -- non-numeric, a float, scientific notation -- all
degrading to the default, not raising); `build_apartment_detail`
unit-tested for the unknown-apartment `None` case, the never-reported
placeholder, gap detection at exactly the threshold (no gap, one collapsed
run) and just above it (a gap plus two runs), the window-edge case above,
caught-up vs. live heartbeat marking (now `caught_up_count`, per run),
run aggregation (a long contiguous run collapses to one row; two runs
separated by a gap give run/gap/run; caught-up counts stay scoped to their
own run, not a running total), a stale last heartbeat producing no
trailing gap row (the still-open interval up to `now` is left to the
alarms section, see above), `days` clamping, open faults from the
latest heartbeat, every battery/signal/version/system/control field, the
outdated-protocol flag (same `monkeypatch.setattr(storage_module,
"PROTOCOL_VERSION", ...)` technique P3.1 uses), and both an open and a
since-cleared alarm; the three new `Storage` methods unit-tested directly
for ordering, the `since` bound, and per-apartment scoping; HTTP-level
tests logging in via the real P3.0 flow for: unauthenticated access
redirecting (303), an unknown apartment 404ing with the same base layout,
every section rendering from real stored data in one combined test, the
titel/text-leak test described above, `days` capping at the HTTP layer
(`?days=9999` -> 14, `?days=0` -> 3) plus the three malformed-`days`
regression tests (`?days=abc`, `?days=3.5`, `?days=1e400`, each asserting
200 with the default heading, never 422), XSS escaping of an apartment id,
zone, mode, and event key all containing `<script>`, the encoded-link
round trip, the section-6 absence check, and the security headers
(`Content-Security-Policy`, `X-Frame-Options`, `Referrer-Policy`,
`Cache-Control: no-store`) -- plus a template-level inline-style/script
check mirroring `tests/test_ui_auth.py`'s existing glob-based one (which
already also covers `apartment.html` automatically, since it globs every
template in the directory).

Verification for this round (run against `main` merged in, P3.4 included):
`ruff check .`, `mypy .`, `mypy protocol fleet agent tools` all clean;
`python -m pytest -W ignore::ResourceWarning` (402 passed, 99% coverage
overall, `fleet/ui_apartment.py` and every other P3.2/P3.4 file at 100%).

## "Aufgaben" -- the fleet UI's tasks view (P3.4, section 9's third view)

`GET /ui/tasks` (behind `require_ui_user`, same as every other `/ui` route)
renders section 9's third view: "what is due: battery rounds, updates,
unconfirmed faults -- the list people actually work from." The "Aufgaben"
nav link in `fleet/templates/ui/base.html`, inert since P3.0, now points
here. Sits on top of P3.1 (`Storage.get_house_overview`, reused unchanged --
no new storage query was needed) and P2.2 (the "not reporting" alarm used to
decide which apartments to skip, see below) -- no new migration, no change
to `protocol/`, `fleet/ui_auth.py`, or `CommandType`.

**Where the pieces live.** `fleet/ui_tasks.py` (new) is the view-model
module, mirroring `fleet/ui_house.py`'s shape: `build_task_overview(storage,
now)` calls `Storage.get_house_overview`, filters, groups, sorts, and
renders every field into the German text `fleet/templates/ui/tasks.html`
prints verbatim -- no business logic in the template. `now` is always
injected by the caller (`fleet/ui_routes.py::tasks` passes
`datetime.now(UTC)`), same pattern as every other `now`-injected module in
this repository.

**Three thresholds, each a named constant citing its own row of section 8's
alarm table -- no threshold invented beyond what that table gives:**

- `BATTERY_LOW_PERCENT = 20` (`fleet/ui_tasks.py`) -- section 8: "Battery
  low: weakest cell under 20%, collected until the battery round." Strictly
  under 20; tested at the boundary (19 included, 20 not).
- `FAULT_OPEN_THRESHOLD = timedelta(hours=2)` -- section 8: "Fault open: one
  of the six kinds, longer than 2 h." Strictly longer than 2 h; tested at
  the boundary (1:59 excluded, 2:01 included).
- **The "Updates" group uses `LatestHeartbeat.outdated` (P2.1, section
  18.2) directly, not section 8's own "version gap" row.** Section 8's
  wording ("more than two versions behind") needs a notion of the *current*
  agent/thermoctl release this service has nowhere at all -- no "latest
  known release" concept exists in `protocol/` or `fleet/storage.py`, and
  building one (a hard-coded version string, a registry lookup, a manually
  maintained "current release" table) was out of scope and not decided by
  the project owner. Listing only the already-existing outdated-protocol-
  version case is a narrower, already-supported signal, not the full
  section-8 rule -- **open point, not invented:** whoever adds a "current
  release" concept to this service (likely alongside stage-2's
  `apply_update`, section 7) should revisit this group to also cover the
  "more than two versions behind" case section 8 actually asks for.

**`silent_devices > 0` is deliberately left out of "Batterierunde."**
Considered and rejected: section 8 gives `silent_devices` no threshold or
alarm row of its own at all (it is not one of the nine rows in the table),
and a silent Zigbee device is a network/connectivity signal, not a battery
one -- a device can go silent for reasons that have nothing to do with its
battery (out of range, powered off, a dead unit needing replacement, not
just "needs new batteries"). Folding it into the same round as a genuinely
low battery would invent a grouping section 8 does not establish, and would
make "why is this apartment on the battery round" ambiguous on the page
itself (a landlord bringing batteries to a visit that actually needed a
different fix). Left out; if a future package wants to surface it, it
should get its own task group, not this one.

**Superseded by P3.4a below (project owner decision, 2026-09-26) -- kept
here only as history.** ~~Apartments already flagged on "Das Haus" are
skipped here entirely -- decided while building this package, the work
package's own "decide, document" instruction.~~ `fleet.ui_tasks._eligible`
used to exclude:

- apartments that have **never reported** (`ApartmentOverview.latest is
  None`) -- there is no heartbeat to read a battery/fault/version value
  from in the first place, so there is structurally nothing to compute a
  task from, not merely nothing worth showing. **Unchanged by P3.4a.**
- ~~apartments with a currently **open "not reporting" alarm**
  (`ApartmentOverview.open_alarm is not None`) -- already the single most
  urgent line on "Das Haus" (P3.1's category 0, ranked above everything
  else); a battery percentage, fault, or version read from a heartbeat that
  stopped updating the moment the apartment went silent is stale by
  definition and would duplicate an already-surfaced, more urgent problem
  under a less urgent heading instead of adding a genuinely new piece of
  work to schedule.~~ **Reversed by P3.4a: this made a genuinely still-open
  task (a weak battery, an open fault) disappear from the one list people
  actually work from at exactly the moment the apartment went silent --
  when a landlord investigating the silence could also act on it.**

## P3.4a -- keep tasks of silent apartments, marked stale (2026-09-26)

**Project owner decision, 2026-09-26.** An apartment with an open "not
reporting" alarm now **stays** in every task group its last known
heartbeat still qualifies it for, instead of being excluded from all three
wholesale. The exact motivating example: apartment 7 last reported battery
5% and a fault open 3 h, then went silent -- it used to vanish from
"Aufgaben" entirely, although both tasks almost certainly still applied.
An apartment that has **never** reported (no heartbeat at all) stays
excluded, unchanged -- there is structurally nothing to compute a task
from in that case, a different situation from "last known data now stale."

**What changed (`fleet/ui_tasks.py`):**

- `_eligible` now only checks `overview.latest is not None` -- the
  "not reporting" alarm check is removed from it.
- `_stale_hint(now, overview)` returns `None` for a fresh apartment, or a
  literal German sentence for one with a currently open "not reporting"
  alarm, naming both the age of the heartbeat the row's value was read
  from and how long the alarm has been open (e.g. "Wohnung meldet sich
  nicht (seit 2 Std.) -- Stand der letzten Meldung vor 3 Std."). Text, not
  colour alone (accessibility) -- the work package's explicit requirement.
- `BatteryTask`/`UpdateTask`/`FaultTask` each gained `stale: bool` and
  `stale_hint: str | None`, set by `_battery_task`/`_update_task`/
  `_fault_task` from `overview.open_alarm is not None` and `_stale_hint`.
- **Fault age for a stale row is still measured against `now`, not against
  the heartbeat's own `received_at`** -- unchanged from P3.4, just now
  reachable for a stale row too: section 8's "Fault open: ... longer than
  2 h" is a rule about how long the fault has actually been open in
  wall-clock time. A fault already 3 h old at last contact, apartment
  silent for 3 h since, is 6 h open *now*, not merely 3 h -- showing the
  smaller number would understate exactly the entry this group exists to
  surface.
- **Sorting is unchanged, and `stale` is deliberately not part of any sort
  key** -- decided while building this package. For the battery round in
  particular, the percentage itself remains the more useful ordering
  signal for someone about to do a battery round: an apartment at 3%
  that has since gone silent is not a *lower* priority than a fresh one at
  15%, arguably a higher one (nobody has been able to check on it since).
  Segregating stale rows to the bottom of each group would bury exactly
  the entries this decision was made to keep visible; a stale row sorts
  exactly where its value places it, just carrying the extra flag/hint.

**Template/CSS (`fleet/templates/ui/tasks.html`,
`fleet/static/ui/fleet-ui.css`):** a stale `<li>` gets a `task-list__stale`
class (CSS-only, a dashed border) plus a second line,
`<span class="task-list__stale-hint">`, printing `task.stale_hint`
verbatim -- the hint is literal text, present in the markup regardless of
whether the CSS marker renders, per the work package's own accessibility
requirement ("text, not colour alone"). No inline `style`/`script`
anywhere, same constraint every other `/ui` template in this repository
already follows.

**Tests.** `tests/test_ui_tasks.py::
test_apartment_with_open_not_reporting_alarm_keeps_its_tasks_marked_stale`
replaces the old exclusion test with the exact example from the work
package (battery 5%, a fault open 3 h before going silent, then an open
"not reporting" alarm): both the battery round and the fault entry are
still present, both `stale`, both hints contain the heartbeat age and
"meldet sich nicht", and the fault's `since_text` is measured against
`now` (6 h, not the 3 h visible in the heartbeat).
`test_fresh_apartment_rows_are_not_marked_stale` checks `stale is False`/
`stale_hint is None` for an ordinary apartment.
`test_silent_apartment_with_fine_values_has_no_task` checks a silent
apartment whose values do not cross any threshold still yields no task --
staleness keeps an already-qualifying row, it never invents one.
`test_never_reported_apartment_excluded_from_every_group` (kept from P3.4,
unchanged) still asserts the never-reported case is excluded.
`test_tasks_view_marks_a_stale_row_with_text_not_colour_alone` is the
HTTP-level check: the `task-list__stale` class, the `task-list__stale-hint`
span, the literal "Wohnung meldet sich nicht" text, and no inline
`style`/`script` anywhere on the page.

Verification for this round: `ruff check .`, `mypy .`, `mypy protocol fleet
agent tools` all clean; `python -m pytest -W ignore::ResourceWarning`
(728 passed, 99% coverage overall, `fleet/ui_tasks.py` at 100%).

**No acknowledge/confirm mechanism exists, on purpose -- open point, not
built here.** The work package's own instruction: an "acknowledge" action on
a fault would be a state-changing `POST` (its own CSRF handling, its own
authorization question, its own persistence -- does acknowledging a fault
on the fault's *current* occurrence carry forward to a later re-occurrence
of the same key, or not?) that needs its own design, not a checkbox added as
a side effect of building the read-only list. `fleet/ui_tasks.py` therefore
lists **every** open fault older than the threshold, unfiltered by any prior
acknowledgement, every time the page is loaded -- "Bestätigen" (confirm) is
named directly in the template's own group heading
("Unbestätigte Störungen") as what the group currently lacks, not hidden
behind a vaguer name. A future package adding this needs at minimum: a new
table or column (which fault, which apartment, acknowledged by whom and
when), a `POST /ui/tasks/faults/{id}/confirm`-shaped endpoint with CSRF like
every other state-changing `/ui` route, and a decision on the re-occurrence
question above -- none of that is decided here.

**What a task entry shows, and what it deliberately does not (section
6/9).** Battery round: apartment id (linked), the weakest battery
percentage, and the heartbeat's age ("Stand vor X"). Updates: apartment id
(linked), agent and thermoctl version, the fixed "veraltete
Protokollversion" text (mirroring P3.1's tile tag). Unconfirmed faults:
apartment id (linked), the fault's German kind label
(`fleet.ui_house.FAULT_KIND_LABELS`, reused unchanged rather than
duplicated), the zone, and "seit X" duration since the fault opened.
**Nothing from section 6** is read or shown -- the same structural guarantee
P3.1 already established (`protocol.heartbeat.OpenFault` only ever carries
`kind`/`since`/`zone`) applies unchanged here; verified directly by
`tests/test_ui_tasks.py::test_tasks_view_contains_no_section_6_data`
(same technique as P3.1's equivalent test: a `tenant_report` fault, checked
for the same five forbidden markers). Event `titel`/`text` are not stored at
all (P1.3) and are not read here either.

**Links use a URL-encoded apartment id, per the work package's explicit
requirement** (`urllib.parse.quote(id, safe="")`, `fleet.ui_tasks
._apartment_href`). **Updated note (P3.2 merged afterward, no longer an
inconsistency):** at the time this package was written, P3.1's
`index.html` still linked with the raw, unencoded id -- noted here as a
pre-existing gap in that package, deliberately not fixed from this one
(per this package's own "keep changes to shared files small" instruction).
P3.2's own cross-review then closed exactly that gap (`index.html`'s tile
link now goes through a Jinja `urlpath` filter, see the P3.2 section
above) -- `fleet.ui_tasks._apartment_href` and P3.2's `urlpath` filter now
both call `urllib.parse.quote(id, safe="")` with the same arguments and
therefore produce byte-identical encoded output; two independently-chosen
mechanisms that agree, not two that need reconciling. `/ui/apartments/{id}`
itself is P3.2's route, built in parallel, not built or tested for
resolution here (the work package: "do not test that the link resolves").

**Sorting, decided while building this package (section 8/9 give no
explicit order among entries of one group):** battery round ascending by
percentage (weakest first -- the whole point of "collected until the
battery round" is knowing which one needs attention soonest); updates
alphabetically by apartment id (no urgency dimension exists between two
outdated apartments); unconfirmed faults oldest-`since`-first ("longest
overdue" first), the same "worst first" reading `fleet/ui_house.py` already
applies to open-fault ordering on "Das Haus", adapted from "most faults" to
"longest open" since this group lists individual faults, not apartments.
Sorted on the actual timestamp, not the rendered "seit X" text (a string
sort of "3 Std." against "50 Min." would be wrong).

**Empty groups.** Each of the three groups renders "Nichts fällig." when
empty, independently of the other two (P3.1's "whoever has nothing to do
sees a quiet surface", carried into this view) --
`tests/test_ui_tasks.py::test_tasks_view_shows_the_empty_state_for_every_group`
asserts the text appears exactly three times for an apartment with nothing
due in any group.

**CSS** (`fleet/static/ui/fleet-ui.css`, `.task-group`/`.task-list`/
`.task-group__empty`) is plain, no colour-coded urgency the way P3.1's tiles
have one -- a task list is already ordered by what needs doing, section 9
does not ask for a second visual layer on top of that, and there is no
per-entry "trouble level" analogous to P3.1's alarm/fault/outdated/never-
reported categories to colour by (every entry in a given group is, by
definition, already something to do). The now-unused `.inactive`/
`span.inactive` nav rule (P3.0's "not built yet" placeholder styling) was
removed along with the placeholder `<span>` it styled.

**Tests.** New `tests/test_ui_tasks.py` (18 tests) against a real, migrated
SQLite database, no mocks, same fixtures/`_login` helper as
`tests/test_ui_house.py`: every threshold boundary named above; sorting
within each group; the never-reported and open-alarm exclusions (unit- and
implicitly HTTP-tested); the `_relative_duration` "< 1 Min." branch (the
other branches are exercised incidentally by the boundary tests); the
URL-encoded href (including a non-ASCII-unsafe id containing `/` and a
space); HTTP-level: unauthenticated access redirecting (303), one entry
rendered per group with its exact text, the empty state for all three
groups at once, an apartment id containing `<script>...` HTML-escaped, the
section-6 absence check, and the security headers including `Cache-Control:
no-store`.

Verification for this round: `ruff check .`, `mypy .`, `mypy protocol fleet
agent tools` all clean; `python -m pytest -W ignore::ResourceWarning`
(360 passed, 99% coverage overall, `fleet/ui_tasks.py` at 100%).

## "Das Haus" -- the fleet UI's house overview (P3.1, section 9's first view)

`GET /ui/` (P3.0's protected placeholder) now renders section 9's first
view: one tile per apartment known to storage, "sorted by trouble, not by
number" -- "whoever has nothing to do sees a quiet surface". Sits directly
on top of P3.0 (`require_ui_user`, unchanged) and P2.1/P2.2 (heartbeats,
alarms) -- no new migration, no change to `protocol/`, `fleet/ui_auth.py`,
or `CommandType`.

**Where the pieces live.** `fleet/storage.py` gained `ApartmentOverview`
(a per-apartment `latest: LatestHeartbeat | None` plus
`open_alarm: AlarmRecord | None`) and `Storage.get_house_overview()` -- the
one storage-level read the work package asked for, so the route/view layer
runs no per-field queries of its own. Query count is `2 + len(apartments)`:
one for the id list, one batched query for every currently open
"not reporting" alarm (`Storage._get_open_not_reporting_alarms`, keyed by
apartment id), and one `get_latest_heartbeat` call per apartment (its own
tie-break ordering is not duplicated here) -- acceptable for "~12
apartments" per the work package, and still one query per apartment, not
one per displayed field. The "not reporting" alarm kind is compared as the
plain string literal `"not_reporting"`, mirroring
`fleet.alarms.AlarmKind.NOT_REPORTING.value` -- `fleet/storage.py` still
does not import `fleet/alarms.py`'s enum types, same "avoid a circular
import" reasoning as `EventRecord.fault_kind`/`AlarmRecord.kind` already
follow.

`fleet/ui_house.py` (new) is the view-model module: `build_house_overview(storage,
now)` calls `Storage.get_house_overview`, sorts it, and renders every field
into the German text/booleans `fleet/templates/ui/index.html` prints
verbatim -- no business logic in the template itself. Deliberately its own
module, not folded into `fleet/ui_routes.py` (which stays the thin HTTP
layer its own docstring describes): P3.2/P3.4 are expected to add their own
`ui_apartment.py`/`ui_tasks.py` next to it, sharing only `base.html`/
`fleet-ui.css`. `now` is always injected by the caller
(`fleet/ui_routes.py::index` passes `datetime.now(UTC)`) -- same pattern as
`fleet/alarms.py`/`fleet/ui_auth.py` -- so every test controls heartbeat/
alarm age exactly, without waiting on a real clock.

**The ordering rule -- a derived reading of section 9, not a spec quote
(worth stating plainly, since the specification itself only sets the goal,
not a concrete rule among several simultaneous kinds of trouble).** Decided
while building this package, documented in both `fleet/ui_house.py`'s
module docstring and here so it survives either file being read alone:

1. an apartment with an **open "not reporting" alarm** (P2.2) -- section
   8's own headline case ("the cloud's value lies in absence, not in
   receiving").
2. apartments with **open faults**, worst (most faults) first.
3. apartments on an **outdated protocol version** (section 18.2).
4. apartments that have **never reported** -- kept separate from category 1
   on purpose: P2.2 does not alarm an apartment with no heartbeat ever
   (open point, this file's "Absence alarming" section below), so there is
   nothing to escalate on yet, but it is still not "fine".
5. everything else -- fine, rendered visually quiet.

Ties within a category are broken by apartment id, so the order is fully
deterministic regardless of query/dict iteration order --
`tests/test_ui_house.py::test_ordering_across_all_five_categories_and_the_id_tie_break`
builds one apartment per category plus a same-category pair and asserts the
exact resulting list.

**What a tile shows, and what it deliberately does not (section 6/9).**
Apartment id (there is no separate "name" in storage, per the work
package -- inventing one was out of scope, not merely postponed); last
contact as receipt time in words ("vor 3 Min.", "vor 2 Std.", "vor 5
Tagen") or "noch nie gemeldet"; mode and "thermoctl erreichbar" from the
latest heartbeat's `thermoctl` block; open faults as a count plus German
labels (`fleet/ui_house.py::FAULT_KIND_LABELS`, a closed mapping covering
every `protocol.heartbeat.FaultKind` member -- a lookup miss raises
`KeyError` rather than silently dropping a fault) and the zone id; agent
and thermoctl version, plus a "veraltete Protokollversion" tag when
`LatestHeartbeat.outdated` is set; an open "not reporting" alarm's
since-when ("seit 10 Min."). **Nothing from section 6** (room temperature,
setpoint, schedule, absence period, tenant name/contact) is read, shown, or
derivable from what is read -- verified directly by
`tests/test_ui_house.py::test_house_view_contains_no_section_6_data`, which
feeds a `tenant_report` fault (the one `FaultKind` whose *event* payload,
not the heartbeat fault, would carry such data) and asserts none of
`"°C"`/`"Sollwert"`/`"Zeitplan"`/`"Mieter:"`/`"Kontakt:"` appear -- the
`OpenFault` model itself only ever carries `kind`/`since`/`zone`, so
there is structurally nothing to leak here by construction, not only by
omission. Event `titel`/`text` are not stored at all (P1.3) and are not
read by this module either.

**"Whoever has nothing to do sees a quiet surface"**
(`.apartment-tile--quiet` vs. `.apartment-tile--trouble` in
`fleet/static/ui/fleet-ui.css`) -- a lower-contrast, unaccented left border
for a fine tile against a red one for anything in categories 0-3. Status is
always also conveyed by text (the alarm line, the "veraltete
Protokollversion" tag, the fault list, "Nein" for unreachable) -- never by
colour alone, per the work package's accessibility requirement. Headings
(`<h1>`/`<h2>`), a `<dl>` of facts, and a plain `<ul>` of faults, no
`<style>`/`<script>`/`style=` anywhere (checked by the pre-existing
`tests/test_ui_auth.py::test_templates_contain_no_inline_style_or_script`,
which globs every template in the directory, this one included).

**Each tile links to `/ui/apartments/{id}`** (P3.2's route, not built
here -- the link 404s until then, expected and left as-is per the work
package: "P3.2 will add that route; do not build it here").

**Tests.** New `tests/test_ui_house.py` (18 tests) against a real, migrated
SQLite database, no mocks: `Storage.get_house_overview` unit-tested
directly (every apartment including never-reported, the open-alarm-only
join, empty-fleet); `fleet.ui_house.build_house_overview` unit-tested for
every tile field, the never-reported placeholder, an open alarm's
since-text, a cleared alarm not flagging the tile, the "quiet" boolean for
a genuinely fine apartment, the outdated flag (via the same
`monkeypatch.setattr(storage_module, "PROTOCOL_VERSION", ...)` technique
`tests/test_storage.py`'s own outdated-flag tests use, since a heartbeat's
`protocol_version` itself must stay `>=1`), `_relative_duration`'s
sub-minute/hour/day branches, and the full five-category-plus-tie-break
ordering; HTTP-level tests logging in via the real P3.0 flow (mirroring
`tests/test_ui_auth.py::_login`) for: unauthenticated access still
redirecting (303) to `/ui/login`, every stored apartment rendered
(including never-reported and an open fault's German label), the open
"not reporting" alarm's text appearing, an apartment id containing
`<script>...` HTML-escaped (`&lt;script&gt;`, not executed), the section-6
absence check above, the security headers (`Content-Security-Policy`,
`X-Frame-Options`, `Referrer-Policy`, `Cache-Control: no-store`) still
present on this now-non-placeholder page, and the empty-fleet state
("Keine Wohnungen registriert.").

Verification for this round: `ruff check .`, `mypy .`, `mypy protocol
fleet agent tools` all clean; `python -m pytest -W ignore::ResourceWarning`
(342 passed, 99% coverage overall, `fleet/ui_house.py` and every other P3.1
file at 100%).

## Login for the fleet UI (P3.0), round-4: reserve-then-verify + XFF hardening + background notification

Re-review of round 3 reproduced one real defect and asked for two cheap,
agreed hardenings. All fixed:

**1. IP-throttle check-then-act race (the real defect).** Round 3's
`login_submit` read `Storage.is_ip_login_blocked` and only wrote
`record_ip_login_failure` *after* `authenticate` had already run and
failed -- a check-then-act gap, the exact same class of bug round 2's
concurrency fixes closed elsewhere, just not yet closed here. Reproduced:
30 concurrent `POST /ui/login` from one IP at throttle threshold 5 -- 30/30
ran Argon2, and 30 failures landed on the *account's* counter, not the
IP's. A single address with ~50 concurrent connections could force the
account-level lock entirely on its own, which is precisely the attack the
per-IP throttle exists to prevent. **Fixed with reserve-then-verify:**
`Storage.reserve_ip_login_attempt` (replacing `record_ip_login_failure`)
atomically increments the IP's attempt counter -- windowed, same model as
the account lock -- *before* `authenticate` runs at all, and returns
whether the resulting count is still within the configured threshold (the
`threshold`-th attempt itself is still allowed through; only attempt
`threshold + 1` onward is refused and skips `authenticate` entirely, no
Argon2, no account-counter credit). `Storage.release_ip_login_attempt`
gives the reservation back (atomic `failures = MAX(failures - 1, 0)`,
never negative) after a request that went on to log in *successfully*, so
a legitimate user is not penalised for their own successful attempt --
deliberately does not touch `window_started_at`/`blocked_until`, since a
release can only ever happen for a request `reserve_ip_login_attempt`
already allowed through, so there is never an active block to reason
about lifting early. `Storage.is_ip_login_blocked` remains as a read-only
status check (useful for inspection/tests/a future status view) but is no
longer part of the request-path decision at all -- `reserve_ip_login_attempt`
is now the single gate.

Regression test:
`tests/test_ui_throttle.py::test_concurrent_login_requests_respect_the_ip_throttle`
-- 30 **real concurrent `client.post("/ui/login")` calls** (not a direct
`Storage` call) from one IP, each with its own `TestClient`/cookie jar (to
avoid a shared-cookie-jar race in the *test* itself clobbering the
pre-session CSRF cookie -- an artifact of the test harness, not the
throttle) but all resolving to the same address (Starlette's fixed
`TestClient` default peer). Spies on `PasswordHasher.verify`'s call count
(same monkeypatch-the-class technique as the round-2 timing-oracle tests)
and asserts it is called **at most `threshold` times**, and that the
account's own `failed_attempts` rises by **at most `threshold`** too. Run
10/10.

**2. `X-Forwarded-For` robustness (cheap, agreed).** `fleet.ui_auth
._strip_port` now strips a `:port` suffix (`203.0.113.9:51413` →
`203.0.113.9`) and RFC 7239-style IPv6 bracket notation
(`[2001:db8::1]:51413` → `2001:db8::1`, `[2001:db8::1]` with no port too)
from each `X-Forwarded-For` entry before it is compared against the
trusted-proxy set or considered as the resolved client address -- without
this, a proxy that appends a port would never match a configured trusted
entry (so its own hop would never be skipped when walking the chain), and
would be used *as the throttle key itself* if chosen, needlessly splitting
one real address across many meaningless throttle-table rows by port
number. A bracket-less IPv6 address with no port (`2001:db8::1`, which has
multiple colons of its own) is correctly left untouched -- the stripping
logic requires *exactly* one colon plus a dot (IPv4) to treat something as
`host:port`. Separately, `resolve_client_ip`'s final chosen candidate --
from the header or the direct peer -- is now validated as a real IP
address (`ipaddress.ip_address`) before being returned at all; an
unparseable result (a malformed/garbage header entry that happens not to
match the trusted set either, or a non-IP `request.client.host` such as a
Unix-socket peer) falls back to the direct peer rather than handing an
arbitrary string to the storage layer as a throttle key. Tests:
`test_resolve_client_ip_strips_an_ipv4_port_suffix`,
`::strips_a_bracketed_ipv6_port_suffix`,
`::strips_a_bracketed_ipv6_with_no_port`,
`::leaves_a_bare_ipv6_without_a_port_unchanged`,
`::falls_back_to_the_direct_peer_for_an_unclosed_bracket`,
`::falls_back_to_the_direct_peer_for_a_non_ip_xff_entry`.

**3. "Account locked" notification moved off the request path (cheap,
agreed).** `fleet.ui_auth.authenticate` gained an optional `background_tasks:
fastapi.BackgroundTasks | None` parameter; when given (always, from
`fleet/ui_routes.py::login_submit`), a just-triggered `notify_ui_account_locked`
call is scheduled via `BackgroundTasks.add_task` instead of being called
inline, so a slow or hanging notifier (a blocking SMTP connection, an
unreachable webhook host) cannot delay the login response itself. `None`
(the default) falls back to the synchronous call exactly as round 3 did --
used by every test that calls `authenticate` directly, outside of any
request/response cycle, where there is no response to avoid delaying.
"Exactly once per lock" is unaffected either way: `Storage
.record_ui_login_failure`'s atomic `just_locked` result still decides
*whether* to notify at all; this parameter only changes *when*, and on
what thread of control, an already-decided notification actually runs.

**Why this needed a white-box test, not a wall-clock one:**
`starlette.testclient.TestClient` drives the *entire* ASGI request cycle,
background tasks included, to completion before `client.post(...)` itself
returns -- confirmed empirically (a 1.0s `time.sleep` background task
still added ~1.0s to `TestClient`'s own call). A timing assertion through
`TestClient` would therefore pass or fail for the wrong reason regardless
of whether the fix is present; it cannot distinguish "scheduled via
`BackgroundTasks`" from "called inline" by wall-clock alone, since both
still block the *test's* call to `.post()` for as long as the notifier
takes. `tests/test_ui_throttle.py
::test_login_submit_schedules_the_notification_via_background_tasks`
instead calls `login_submit` directly as a plain Python function (bypassing
FastAPI's request/dependency machinery, which is only needed when going
through the ASGI app) with a real `BackgroundTasks()` instance, and
inspects it *immediately after `login_submit` returns* -- confirming the
notifier has not run yet (`notifier.raw_calls == []`) and is only queued
(`background_tasks.tasks[0].func is notify_ui_account_locked`), then runs
the queued task manually (`asyncio.run(background_tasks())`, exactly what
Starlette does after sending the real response) and confirms it fires
then. A second, end-to-end test,
`::test_login_response_does_not_wait_on_the_notifier_even_when_it_is_slow`,
drives the real ASGI app through `TestClient` with an injected notifier
delay (0.05s, "no real sleeping longer than a fraction of a second" per
the work package) purely to prove the wiring does not hang, error, or
double-fire under the real app -- not to assert on timing, for the reason
above.

Verification for this round: `ruff check .`, `mypy .`, `mypy protocol
fleet agent tools` all clean; `python -m pytest -W ignore::ResourceWarning`
run three times (323 passed each time, 99% coverage, every P3.0 file at
100% except `fleet/admin.py`'s untested `__main__` guard, unchanged from
earlier rounds); every concurrency regression test in the P3.0 suite
(`test_totp_replay_race_allows_exactly_one_concurrent_login`,
`test_concurrent_failed_logins_do_not_lose_counter_updates`,
`test_concurrent_failed_logins_lock_the_account_deterministically`,
`test_concurrent_ip_failures_do_not_lose_updates`,
`test_concurrent_ip_failures_block_deterministically`,
`test_concurrent_lockout_notifies_exactly_once`, and the new
`test_concurrent_login_requests_respect_the_ip_throttle`) run 10 times
individually, 10/10 passes every time.

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
`0005_ui_accounts`).** **Superseded by round 4's reserve-then-verify fix,
see the section above this one for the current design and the race this
paragraph's original design had** -- kept here only as history. One row
per IP address that has ever failed a login: `failures`, `window_started_at`,
`blocked_until`. Default 5 failures within 15 minutes blocks that one IP
for 15 minutes; the window resets (fresh count) once it lapses with no
failures. A successful login does **not** unblock any IP, including its
own -- an already-blocked window simply lapses on its own; there is no
special-case reset path that could itself become a bug (this part is
still true after round 4). `Storage.reserve_ip_login_attempt`/
`release_ip_login_attempt` are the current entry points (round 4);
`is_ip_login_blocked` is now read-only, not part of the request-path
decision. Regression tests (still passing, exercising the current
methods): `tests/test_ui_throttle.py::test_concurrent_ip_failures_do_not_lose_updates`,
`::test_concurrent_ip_failures_block_deterministically`,
`::test_ip_throttle_concurrency_regression_runs_reliably` (10 rounds) --
plus round 4's own `test_concurrent_login_requests_respect_the_ip_throttle`
for the check-then-act race specifically.

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
`MAX_CATCH_UP_HEARTBEATS = 240`. **Gap detection for caught-up heartbeats
(section 5, "The cloud detects gaps by the timestamp and displays them as
such") was left open here on purpose** -- this package stores the batch;
*displaying* a detected gap is a UI concern that belongs with P3.2 (the
apartment detail view), not this endpoint. **Closed by P3.2**, see this
file's own "Eine Wohnung" section at the top for
`fleet.ui_apartment._build_timeline`'s gap/caught-up derivation.
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
