# Status

Last updated: 2026-09-28.

## Owner decisions for P5.4 and P5.5b (2026-09-28, main session)

Recorded in `docs/specification.md` (sections 13 and 15.3, "Decided
afterward") and `docs/implementation_plan.md`:

- **P5.4** is built now but stays inactive: fail-closed pre-check
  (unreadable thermoctl health/outdoor temperature rejects) plus
  `pilot_mode` required at the target. No new `CommandType`; `apply_update`
  stays stage 2. **Open point for P5.4:** `pilot_mode` currently lives only
  in the fleet inventory; the agent has to learn it from the cloud, so this
  gate protects against *accidental* arming, not against a compromised
  cloud -- that protection stays with the hard-coded sources, the digest
  check, and the local pre-check (security principles 2 and 5).
- **P5.5b follow-up decisions (2026-09-28):** (a) the browser-JS limit is
  accepted and documented (spec 15.3) -- protects against DB leak, logs,
  backups, passive compromise, not against an actively taken-over fleet
  server; the UI shows the script's sha256. (b) The agent writes restores
  only into its own staging directory; thermoctl/Zigbee2MQTT mounts stay
  read-only; a separate small Go program next to the watchdog (no deps, no
  network) moves staged data into place, only onto an empty device.
- **P5.5b** restore key: encrypted in the landlord's browser to a
  device-generated age recipient; the fleet stores and forwards only the
  opaque block and deletes it after fetch or expiry.
## Merge integration: P5.3b onto P5.1c (main session)

- Migration re-chained: P5.3b's `0012_diagnostic_bundles` is now
  `0013_diagnostic_bundles` (`down_revision = "0012"`, P5.1c's
  `0012_fleet_epoch`). Verified: `upgrade` from empty, `downgrade` to
  `0012`, `downgrade` to `base`, `upgrade` again. `PROTOCOL_VERSION` stays
  6 (main was 5).
- `tests/test_agent_diagnostic_bundle.py`'s module-scoped real-server
  fixture had the same ordering gap P5.1c fixed in
  `tests/test_agent_fetch_logs.py`: the server started with no storage
  override, and since P5.1c the epoch rotation at startup aborts loudly by
  design -- `test_diagnostic_bundle_end_to_end_success` failed with
  "Connection refused". Fixed the same way (module-scoped bootstrap storage
  registered before `fleet_base_url`); test-only, production unchanged.
  These two files are the only module-scoped `uvicorn.Config` fixtures.
- Verification (main session): `ruff check .` clean, `mypy .` (110 files)
  and `mypy protocol fleet agent tools` (58) clean, pytest twice: 1357
  passed, 1 skipped, TOTAL 5260 stmts / 20 missed / 99% both runs;
  `go vet`, `go test`, `check_contract.sh` passing.

## P5.3b -- end-to-end encrypted diagnostic bundle (sections 15.1, 21.5)

`diagnostic_bundle` is now genuinely executed, per the project owner's
2026-09-27 decision: **full content, end-to-end encrypted on the device with
the landlord's public keys -- the exact same mechanism as the operational-data
backup (`agent/encryption.py`, no second procedure)**, never a series (the
distinction section 21.5's own "Decided afterward" paragraph draws between a
bundle and a channel).

**Agent** (`agent/loop.py`): `create_diagnostic_bundle` collects, into a
private staging directory (`tempfile.mkdtemp`, `chmod 0700`, every file in it
`chmod 0600`) built directly under `BackupConfig.staging_dir`:

- **Logs of the four services** (`thermoctl`, `zigbee2mqtt`, `mosquitto`,
  `agent`) -- read from the local Docker Engine API over the Unix socket
  (`read_container_log_window`, the same "no Docker SDK, local socket only,
  never a registry" reasoning `agent.loop.read_container_log_lines` already
  established for `fetch_logs`, P5.3a), bounded to the last **6 hours**
  (`DIAGNOSTIC_BUNDLE_WINDOW_HOURS`, a fixed constant, deliberately **not** a
  caller-supplied parameter -- widening it is exactly the "series instead of
  a snapshot" shift section 21.5 forbids) and capped per service at **2000
  lines / 2 MB** (`DIAGNOSTIC_BUNDLE_MAX_LINES_PER_SERVICE`/
  `_MAX_BYTES_PER_SERVICE`), whichever is hit first; truncation is recorded
  in the manifest, never silent. **Unfiltered, unlike `fetch_logs`'s own
  allowlist** -- this bundle is end-to-end encrypted end to end, so masking
  would only discard exactly the detail a real troubleshooting session needs,
  for no confidentiality gain the encryption does not already provide (the
  project owner's own framing: "a cumbersome path leads to weakening the
  filter instead", applied here to content instead of to the download UX).
  A single service's log (or state) read failing does **not** abort the
  whole bundle -- recorded as `"available": false` with the error text in
  the manifest instead, and the corresponding `services/<name>.log` file
  gets a one-line explanation; a diagnostic tool that refuses to produce
  anything just because one of four services is down would defeat its own
  purpose.
- **Versions/digests**: the watchdog's own self-swap `agent_digest`/
  `agent_proven_digest`, exactly the same reader and the same honest
  limitation `_build_device_config_snapshot` (P5.5a) already documents --
  the four service desired-state digests need P5.4's reconciliation, not yet
  built, so nothing is invented for them.
- **Container states**: `read_container_state` (`GET /containers/{name}/json`
  over the same local socket) -- status, start time, restart count, health
  check status, running image.
- **Memory/disk usage**: `/proc/meminfo` and `shutil.disk_usage("/")`,
  stdlib only (no new dependency such as `psutil`) -- both degrade to
  `None`/best-effort on a platform without `/proc` (this development
  machine) rather than fabricating a value.
- **Zigbee network state, "if cheaply available" read literally**:
  Zigbee2MQTT's own already-written `state.json`, verbatim, capped at 500 kB
  -- **not** a fresh MQTT round trip to the broker, exactly the "cheap" case
  the specification names. Its own outcome (included, absent, unreadable,
  too large) is always recorded in the manifest.
- **Control decisions, honestly scoped down**: this scaffold has no
  thermoctl endpoint that exposes control decisions as their own structured
  feed (section 10's "what needs to change in thermoctl" does not yet list
  one) -- the manifest's `control_decisions` entry says exactly that,
  `available: false`, pointing at `services/thermoctl.log` within the same
  bundle instead of duplicating an extraction this scaffold cannot honestly
  perform yet ("if nothing is available without new thermoctl endpoints, say
  so and include what exists" -- the work order's own words).

**Recipients validated first, before anything else is read or written**
(security principle 4, mirrors `create_backup`'s own ordering exactly) --
`agent.encryption.load_recipients`/`encrypt_stream`, reused directly, not
duplicated. Every intermediate plaintext file (the whole staging directory,
the plaintext tar) is removed unconditionally in a `finally` block,
**including on every error path**. The whole plaintext tar is checked
against a defense-in-depth cap (`DIAGNOSTIC_BUNDLE_MAX_TOTAL_BYTES`, 20 MB)
before encryption -- refused with a clear `ValueError`, not silently
truncated, if ever exceeded (the per-service caps already bound this in
practice). `_handle_diagnostic_bundle` reuses `ExecutionContext.backup_config`
entirely (`apartment_id`, `agent_version`, `staging_dir`, `zigbee2mqtt_dir`,
`client`, `recipients_file`) -- **no second, duplicated configuration
object**, mirroring `_handle_backup_now`'s own shape; every failure
(`RecipientsError`, an unanticipated file/tar/cryptography error, a refused
upload) is an honest failed `CommandResult`, never a fabricated success, and
the staged artifact is always removed in this handler's own `finally`.

**Protocol**: `protocol/diagnostics.py` (new) --
`DiagnosticBundleUploadAccepted`, `MAX_DIAGNOSTIC_BUNDLE_UPLOAD_BYTES` (50 MB,
independent of `protocol.backups.MAX_BACKUP_UPLOAD_BYTES`). A deliberate
separate module from `protocol.backups`, not a third `BackupKind` -- a
diagnostic bundle is not part of the "swap a device in minutes" flow, carries
no retention rotation, and is keyed by *command id*, not by
`(apartment, kind)`. `PROTOCOL_VERSION` bumped to 6 (a wholly new module,
same "counts as a change to the models" reading every prior bump already
established).

**Fleet**: `POST /v1/commands/{id}/bundle` (`fleet/app.py::upload_diagnostic_bundle`,
agent token, `require_apartment_token_by_hash`) -- the command named by the
path `id` must belong to the authenticated apartment and be a
`diagnostic_bundle` command (otherwise 404, deliberately indistinguishable
from "unknown", mirroring `receive_log_excerpt`'s own reasoning), one bundle
per command (`409` on a second upload), the body must look like a real age
file (header line + a recipient stanza -- unconditionally, unlike
`upload_backup`'s own kind-dependent check, since a diagnostic bundle has
only ever one possible kind). **Reuses P5.5a's own streaming-cap and
age-plausibility mechanics, factored out rather than copied**: new
`fleet/upload_streaming.py` (`stream_upload_body`, `looks_like_an_age_file`,
`AGE_PLAUSIBILITY_PREFIX_BYTES`) is now the single home for both, and
`fleet.app.upload_backup` was refactored to import from there too --
identical behaviour, one implementation. Storage:
`fleet/bundle_storage.py::DiagnosticBundleBlobStorage` (new, a deliberate
sibling of `BackupBlobStorage`, not a shared class -- see that module's own
docstring for why a bundle's *command-id* keying does not fit the
*apartment+kind* shape `BackupBlobStorage` uses), `diagnostic_bundles` table
(`fleet/migrations/versions/0013_diagnostic_bundles.py` -- originally `0012`,
chained onto `0011_backups.py`, the head when this package started; P5.1c's
own `0012_fleet_epoch.py` landed on `main` in parallel, also numbered
`0012`, so this migration was re-chained onto it and renumbered `0013` at
merge time, the same main-session convention P5.3a/P5.5a's own parallel
`0010` collision already established; `command_id` carries its own unique
index, enforcing "one bundle per command" at the database level, not only
in application code),
`fleet.storage.Storage.store_diagnostic_bundle`/
`get_diagnostic_bundle_for_command`/`get_diagnostic_bundle_for_apartment_command`/
`get_diagnostic_bundle_storage_path`/`delete_expired_diagnostic_bundles`.

**Retention** (project owner condition, mirrors P5.3a's own `fetch_logs`
retention exactly): 14 days by default, both the window
(`FLEET_DIAGNOSTIC_BUNDLE_RETENTION_DAYS`) and the check interval
(`FLEET_DIAGNOSTIC_BUNDLE_RETENTION_CHECK_INTERVAL_S`) env-configurable, run
periodically from `fleet/app.py`'s own lifespan
(`_diagnostic_bundle_retention_loop`) -- the same "thin scheduling wrapper,
tested via an injected clock on the logic it calls" pattern the existing
alarm/backup/log-retention loops already establish. A bundle's content lives
on the filesystem, not in the database (like a backup, unlike a log
excerpt) -- `Storage.delete_expired_diagnostic_bundles` deletes the metadata
rows and returns each one's `storage_path`; the blob itself is deleted only
after that transaction commits (mirrors `run_backup_retention`'s own "row
first, then blob" ordering). **This retention governs only the fleet's own
stored copy** -- the bundle stays a one-off snapshot per command regardless;
retention prevents old snapshots from *accumulating* into the "series"
section 21.5 forbids, it does not itself enforce the "one at a time" rule
(the database's unique index on `command_id` already does that,
independent of retention).

**UI** ("Eine Wohnung", `fleet/ui_apartment.py`/`fleet/ui_routes.py`): shown
**next to its own command** in the "Befehle" history (unlike backups, which
get their own list-of-many section) -- size, capture time, the SHA-256
content hash, a download link
(`GET /ui/apartments/{id}/commands/{command_id}/bundle/download`, behind the
P3.0 login, scoped to the apartment -- another apartment's command id is a
404), and the ready-made
`age -d -i <dein-schluessel.txt> -o diagnose.tar <datei>` command right
there (project owner: "a cumbersome path leads to weakening the filter
instead" -- the exact wording the project owner specified for this button).

**Constraints honoured**: `protocol.commands.CommandType` unchanged
(`DIAGNOSTIC_BUNDLE` already existed since P5.2); `watchdog/` untouched (`go
vet`/`go test`/`check_contract.sh` all still pass); no private key anywhere
in the repo, the image, the agent, or the fleet -- every identity generated
in this package's own tests is freshly generated at test runtime
(`pyrage.x25519.Identity.generate()`), never committed.

**Tests** (`tests/test_agent_diagnostic_bundle.py`,
`tests/test_fleet_diagnostic_bundle.py`,
`tests/test_storage_diagnostic_bundles.py`, `tests/test_bundle_storage.py`,
`tests/test_ui_diagnostic_bundle.py`, `tests/test_agent_docker_socket.py`
(new, cross-review addition, see below), `tests/docker_api_support.py` (new,
not a test file itself); plus small updates to `tests/test_agent_loop.py`,
`tests/test_agent_loop_execution.py`, and `tests/test_fleet_backups.py`, see
below) -- real cryptography throughout, never a mock of `pyrage`/age: two
freshly generated X25519 identities, each decrypts alone; a marker string
planted in fake service logs/state is asserted absent from every staged
plaintext file, every uploaded request body, and every stored blob, in both
the success and every failure case (missing recipients, only one recipient,
an encryption failure); a single service's log/state read failing is proven
to still produce a bundle for the other three, with an honest per-service
note; an oversized plaintext bundle (monkeypatched cap) is refused before
encryption, cleanly, with every plaintext file removed; the fleet endpoint
rejects a non-age upload (with and without a recipient stanza), a
content-hash mismatch, an oversized body (both a declared-`Content-Length`
check and a genuinely streamed-oversized body with no `Content-Length` at
all, mirroring P5.5a's own two-pronged streaming-cap tests), another
apartment's command, a non-`diagnostic_bundle` command, and a second upload
for the same command (`409`, first upload's content unchanged); retention is
proven with an injected clock (only expired rows/paths returned, fresh ones
untouched); the UI download route requires login, returns the exact stored
bytes unmodified, and is 404 for an unknown or another apartment's command
id; handler-level tests prove `_handle_diagnostic_bundle`'s own two
preconditions (`backup_config`/`client` missing) and that an unanticipated
`create_diagnostic_bundle` failure is caught, reported as a failed result
(never propagated to crash the agent's main loop), with cleanup still having
run; one true end-to-end test drives the command through `execute_command`
against the real `fleet.app.app`, with stub (never real Docker-socket)
log/state readers standing in for the four services, and proves the stored
blob decrypts, with either generated identity, to a tar containing the
manifest.

**Cross-review addition: a real Docker-socket test double, plus two
directly-testable branches the first draft of this section incorrectly
called "unreachable".** Cross-review confirmed the plaintext path, the
shared-streaming reuse, apartment/command-type/duplicate scoping, the fixed
6-hour window, and the migration chain, but required two fixes:

1. **`tests/docker_api_support.py`** (new) -- a small, real Docker Engine
   API double (`FakeDockerAPI`, a raw ASGI callable, no FastAPI/Starlette
   dependency needed for two routes) served by `uvicorn` over a **real Unix
   domain socket**, short-named directly under the system temp directory
   (not pytest's own nested `tmp_path`, whose path can exceed
   `sockaddr_un.sun_path`'s own length limit). **`tests/test_agent_docker_socket.py`**
   (new) uses it to exercise `read_container_log_lines` (P5.3a),
   `read_container_log_window`, and `read_container_state` (both P5.3b)
   against a real socket and a real HTTP response for the first time -- not
   only via the stub readers every other test in this package (and P5.3a's
   own `tests/test_agent_fetch_logs.py`) deliberately uses instead: the
   normal case; byte-cap truncation where a single real chunk read from the
   wire already overruns the cap (`FakeDockerAPI`'s single-`bytes` logs
   value); byte-cap truncation where the cap is exactly reached by an
   earlier chunk and a later, separate chunk must be discarded entirely (a
   distinct branch in `read_container_log_window`'s own read loop --
   `FakeDockerAPI`'s `logs` value can also be a `list[bytes]`, sent as
   genuinely separate ASGI body frames with a short sleep between them, so
   the two chunks are not coalesced into one read on the client side); the
   line-cap truncation, keeping the newest lines; an unknown container
   (`404` -> `httpx.HTTPStatusError`, an `HTTPError`); and a connection
   error (no socket listening at all).
2. **Two branches this section's first draft called unreachable were, in
   fact, directly testable, and are now tested**: `_read_disk_usage`'s own
   `except OSError: return None` (`shutil.disk_usage` on a path that does
   not exist, no monkeypatching needed) and `_read_memory_usage`'s
   malformed-value `except ValueError: continue` (extended the existing
   `Path.read_text`-monkeypatched `/proc/meminfo` fixture with one
   deliberately non-numeric value line, and asserted the earlier, valid
   value for the same key survives unharmed). Both were only ever
   "unreachable *from `create_diagnostic_bundle`'s own call sites* on this
   development machine" -- calling either function directly with a
   constructed input reaches them without any artificial trick, which is
   exactly what a direct unit test is for; the original wording
   conflated "not naturally reached by this package's own higher-level
   tests" with "not testable", which cross-review correctly rejected.

**Small, adjacent test/doc fixes, not weakening what any existing test
protects:**

- `tests/test_agent_loop.py`/`tests/test_agent_loop_execution.py`:
  `create_diagnostic_bundle`'s own former "reports missing implementation"
  stub test removed (the function is no longer a stub); the
  `DIAGNOSTIC_BUNDLE` case removed from the shared "not yet available"
  parametrization and replaced with a dedicated "refused, honestly, when
  `ctx.backup_config is None`" test, mirroring `backup_now`'s own existing
  precedent exactly.
- `tests/test_fleet_backups.py`: the two direct unit tests of the
  streaming-cap function (`test_stream_backup_body_stops_reading_...`/
  `test_stream_backup_body_reads_every_chunk_...`) now import
  `fleet.upload_streaming.stream_upload_body` instead of the now-removed
  `fleet.app._stream_backup_body` -- same behaviour, same tests, only the
  import path changed to follow the P5.3b refactor.

**Verification** (fresh venv, `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"`): `ruff check .` -- `All checks passed!`; `mypy .` --
`Success: no issues found in 109 source files`; `mypy protocol fleet agent
tools` -- `Success: no issues found in 57 source files`; `python -m
tools.check_image_config` -- `Image configuration plausible (not a real
build -- see docstring).`; `python -m pytest -W ignore::ResourceWarning -rA`
**2x**, both exit code 0, **1316 passed, 1 skipped** each run (the one skip:
no `age` CLI binary on this machine, same pre-existing case every prior
package's own verification already notes), coverage **99%** both runs (5185
statements; run 1: 20 missed; run 2: 19 missed) -- the one-line difference is
`fleet/storage.py`'s own pre-existing inventory-assignment concurrent-race
branch (line 3336), the same timing-based coverage wobble this file's own
P5.1b cross-review entry already documents for a different race branch,
unrelated to this package. Every remaining miss in both runs is pre-existing
and unrelated to this package (`agent.loop`'s own still-open
`collect_heartbeat`/`send_heartbeat` placeholders, P5.3a's own
`_handle_fetch_logs` transport-error branch, `reconcile_desired_state`'s own
placeholder, `fleet/admin.py`'s own pre-existing gap, `tools
/check_image_config.py`'s own pre-existing CLI-entry-point gaps) --
**none of this package's own new code remains uncovered**, including the
two branches point 2 above corrects and the real Docker-socket bodies of
`read_container_log_window`/`read_container_state`, now exercised for real
by `tests/test_agent_docker_socket.py` rather than only via stub readers;
that test file also gives `read_container_log_lines`'s own real-socket body
(P5.3a) its first real-socket coverage. In `watchdog/`: `go vet ./...`
clean, `go test ./...` -- all three packages `ok`, `bash check_contract.sh`
-- "Contract test passed" (run with the `agent` extra installed, same as
every prior package since P5.5a -- the watchdog's own `go.mod` has and needs
no new dependency, untouched by this package).

## P5.1c -- SSE resume survives a fleet database restore (sections 3, 7)

**The problem** (found during the P5.E end-to-end run, 2026-09-28): the SSE
event id is `commands.id`, a single autoincrement counter shared by every
apartment. The server only ever honours a `Last-Event-ID` if it names one
of *this* apartment's own command sequences (P5.1's cross-review
membership fix, above). That check is correct as far as it goes, but it
cannot see the fleet database being **reset or restored from an older
backup** underneath it: sequence numbers restart, and a sequence an agent
already persisted can be *reused* by a brand-new, unrelated command for
the same apartment -- which *does* pass the membership check (it really is
one of this apartment's own sequences now), so the new command is silently
skipped instead of delivered. In the E2E run's scenario (f) this hung for
hours.

**Fix: a random, stable epoch id, created once per database lifetime.**
New migration `fleet/migrations/versions/0012_fleet_epoch.py`
(`down_revision="0011"`), a one-row `fleet_epoch` table (`FleetEpochRecord`
in `fleet/storage.py`): `id` pinned to `1` (a real primary key, so a second
row can never exist), `epoch` a fresh `secrets.token_hex(16)` generated
**at migration time** (an empty, just-migrated database gets its own epoch
immediately), `created_at` for operator visibility only.

`Storage.get_epoch()` reads it back (defensive get-or-create if the row is
somehow missing -- never a crash, falls back to creating a fresh one, the
same "fail toward the safe default" reasoning `_last_event_id` already
applies to a malformed header). `Storage.rotate_epoch(now)` replaces it
with a fresh random value -- the operator-facing half, `python -m
fleet.admin rotate-epoch` (new CLI subcommand, `fleet/admin.py`).

**Fleet side (`fleet/app.py`):** the SSE event id is now `<epoch>.<sequence>`,
not a bare sequence (`_stream_command_events`, threaded through from
`commands_stream`'s own `epoch = await asyncio.to_thread(storage.get_epoch)`,
read once per connection/request). `_last_event_id(request, current_epoch)`
parses `Last-Event-ID` against `_EVENT_ID_PATTERN`
(`^([0-9a-f]{32})\.([0-9]{1,20})$`) and only returns the sequence part if
the epoch part equals `current_epoch` -- anything else (an old, bare
pre-P5.1c integer; a mismatched epoch; garbage; a header-injection
attempt) falls back to `0`, exactly like every other invalid value this
function already handled, never a 500. `wait=0` is unchanged otherwise --
it still does not advance the bookmark, still honours `Last-Event-ID` the
same way. `Storage.pending_commands`'s own membership check (P5.1
cross-review) is completely unchanged -- the epoch check only decides
whether the sequence part is even worth asking that question about; the
two checks are orthogonal and stack.

**Agent side (`agent/commands_channel.py`):** `_read_last_event_id`
already treated the persisted value as an opaque string (verified while
reading this package's own brief -- it was never parsed as an integer
anywhere in this module). Added: a bound before the value is ever
returned to a caller that sends it in an HTTP header --
`_is_bounded_last_event_id` (charset `[0-9a-f.]`, length ≤
`_MAX_LAST_EVENT_ID_LENGTH` = 64) rejects a too-long or wrong-charset
value (including a CR/LF header-injection attempt), falling back to
`None` -- "no bookmark persisted", exactly the same as a genuinely absent
file, never propagated as an error. Deliberately does **not** hard-code
the fleet's own `<epoch>.<sequence>` shape here -- only the properties
actually needed to rule out header injection, since this module has no
business assuming the exact format will never change server-side again.

**Operator note (updated by cross-review -- see the fixes section below):**
the epoch now rotates **automatically on every fleet service start**
(`fleet.app.lifespan`, before any request is served,
`Storage.rotate_epoch`), not only via the manual CLI. A restart always
accompanies a restore (there is no way to swap the database file under a
running process), so this alone already covers the restore case
unconditionally -- an operator no longer needs to remember a separate
step. The cost is that **every** restart (not only a restore) makes every
currently-connected agent redeliver its still-pending commands once on its
next reconnect, which `Storage.pending_commands`'s own idempotent-
redelivery reasoning already makes harmless (P5.1's own original design).
The manual `python -m fleet.admin rotate-epoch` command stays useful for
the one case the automatic path does not cover: restoring a backup file
into a database whose fleet service process is deliberately kept running
throughout the restore (e.g. a warm standby instance that process is not
itself) -- run it once, by hand, for that case only.

**Multi-process note:** if more than one fleet service process is ever run
against the same database (not currently how this service is deployed --
`docker/Dockerfile.fleet` is one container, one process), each process's
own start rotates the epoch again, invalidating the `Last-Event-ID` every
agent connected to any *other* process was holding -- harmless for the
same reason a single restart is: every affected agent simply redelivers
its still-pending commands on its next reconnect, never silently skips
one.

**No `protocol/` change, `PROTOCOL_VERSION` unchanged** -- confirmed
while implementing: the SSE `id:` field is SSE/transport metadata (the
reconnection mechanism section 3 already describes), never part of
`protocol.commands.Command` or any other wire model; nothing in
`protocol/` references it.

**Tested:** epoch created once, stable across repeated reads, across
separate `Storage` instances, and (implicitly, since it lives in the
database, not in process memory) across a process restart
(`tests/test_storage.py`); the defensive get-or-create if the row is
missing; `rotate_epoch` replaces the value and is visible from a separate
`Storage` instance (`tests/test_storage.py`); migration 0012 up/down and
`compare_metadata` against `0001`-`0012` (`tests/test_storage.py`).
HTTP/SSE level (`tests/test_fleet.py`): `wait=0` and the direct
`_stream_command_events` generator both carry the epoch-prefixed id; a
current-epoch `Last-Event-ID` with the apartment's own sequence resumes
correctly; a bare pre-P5.1c integer id, a wrong-epoch id, and a
parametrized set of garbage/header-injection attempts (empty string, a
lone dot, a too-short/too-long/uppercase epoch, a negative sequence, a
CR/LF injection attempt, a missing sequence) all fall back to `0`, never a
500; **the core reproduction**
(`test_commands_stream_sse_survives_a_simulated_restore_new_command_not_skipped`):
persist a bookmark from one database, "restore" (a brand-new, separately
migrated database at a different path -- its own fresh epoch, its own
`commands.id` counter starting back at 1), create a new command that
reuses sequence 1, confirm it is delivered, not skipped. CLI
(`tests/test_admin.py`): `rotate-epoch` replaces the epoch and reports it;
missing `FLEET_DATABASE_URL` exits `2`. Agent
(`tests/test_agent_commands_channel.py`): the persisted `<epoch>.<sequence>`
value round-trips opaquely; a parametrized set of out-of-bounds/injection
values (too long, CR/LF, uppercase, embedded whitespace, a NUL byte) are
all refused and treated as no bookmark; a value at exactly the length
ceiling is still accepted; `_stream_once` never sends an invalid persisted
value as a header at all; the existing end-to-end SSE tests updated to
assert the epoch-prefixed persisted value
(`test_receive_commands_holds_an_sse_connection_and_receives_a_command`).

**Verification** (fresh venv, `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"`): `ruff check .` and `mypy .` / `mypy protocol fleet
agent tools` all clean; `python -m pytest -W ignore::ResourceWarning -rA`
**2x**: **1278 passed, 1 skipped** (the pre-existing `age` CLI skip)
identical both runs, coverage **99%** (4889 statements, 25 missed)
identical both runs -- `fleet/app.py`, `fleet/storage.py`'s new epoch
methods, `fleet/admin.py`'s `rotate_epoch`,
`fleet/migrations/versions/0012_fleet_epoch.py`, and
`agent/commands_channel.py` all at 100% for this package's own new/changed
lines (checked via a targeted `--cov` run against exactly those modules).
`watchdog/` and `protocol/` untouched by this package (`git diff --stat`
confirms).

## P5.1c cross-review fixes: a `get_epoch` insert race, `$`-anchored regexes, and automatic epoch rotation on every service start

Cross-review of P5.1c (commit `19a0371`) confirmed the core restore
reproduction works, and required three fixes, all made in the same
follow-up commit.

1. **`Storage.get_epoch()`'s get-or-create had an unguarded insert race**
   (reproduced deterministically, not via real threads): two sessions can
   both observe the `fleet_epoch` row missing before either has inserted
   it; the loser's own insert then raised `IntegrityError` (a primary-key
   violation on the pinned row id) straight out of the method, which
   propagated all the way to a `500` on `GET /v1/commands`. **Fixed:**
   `get_epoch` now reads first; only if the row is missing does it attempt
   an insert, wrapped in its own `try/except IntegrityError` -- the loser
   simply re-reads and returns the winner's row instead of raising. Which
   value wins the race does not matter (any freshly generated epoch is
   equally valid); only "some value, decided once, database-wide" does.
   **Tested** (`tests/test_storage.py::
   test_get_epoch_recovers_when_a_concurrent_session_wins_the_insert_race`):
   `_generate_epoch` is monkeypatched so that, on its first call --
   already inside `get_epoch`'s own insert, after its own read already
   found nothing -- a *second*, independent `Storage` instance commits a
   competing row first, guaranteeing the method's own subsequent insert
   loses the race; asserts the method still returns the winner's value
   rather than raising.

2. **`_EVENT_ID_PATTERN` (`fleet/app.py`) and `_LAST_EVENT_ID_PATTERN`
   (`agent/commands_channel.py`) were `$`-anchored and matched with
   `.match()`.** `re`'s `$` matches "end of string, or just before a
   trailing newline" (without `re.MULTILINE`), so a value like `"<32 hex
   epoch>.<sequence>\n"` incorrectly matched despite not being the exact
   header/persisted value. **Fixed:** both patterns dropped their `^`/`$`
   anchors and are now matched with `.fullmatch()`, which requires the
   *entire* string to match, trailing newline included. **Tested:**
   `tests/test_fleet.py::
   test_commands_stream_wait_0_current_epoch_with_a_trailing_newline_is_treated_as_0`
   (uses the *real*, current epoch plus a trailing `\n`, not a wrong one,
   so the test actually exercises the `.fullmatch()` fix rather than the
   unrelated epoch-mismatch path) and a `\n`-suffixed case added to the
   existing garbage/injection parametrization; agent side,
   `tests/test_agent_commands_channel.py::
   test_is_bounded_last_event_id_rejects_a_trailing_newline` (exercised
   directly against `_is_bounded_last_event_id`, not through
   `_read_last_event_id`, which already strips whitespace off whatever it
   reads from disk before this check ever runs -- stripping would hide the
   bug the direct test is there to catch).

3. **The epoch now rotates automatically on every fleet service start**
   (`fleet.app.lifespan`, `Storage.rotate_epoch`, before any request is
   served), on top of the manual `rotate-epoch` CLI -- **main-session
   decision, cross-review**: restoring an *older* backup of a database
   that was never manually rotated brings the *same* epoch back, and a
   forgotten manual step after restoring would silently reintroduce
   exactly the "reused sequence looks like a legitimate resume point"
   skip this whole package exists to prevent. Restoring a backup always
   involves stopping and restarting the fleet service around the swap (no
   way to replace the database file under a running process), so rotating
   unconditionally on every start closes that gap without depending on
   anyone remembering a separate step -- at the cost of every restart
   (not only a restore) causing every currently-connected agent to
   redeliver its still-pending commands once, harmless by
   `Storage.pending_commands`'s own reasoning. The manual command stays
   for the one case the automatic path does not cover: a restore into a
   database whose fleet service process is deliberately kept running
   throughout. See the updated operator note and multi-process note above
   (also in `fleet.app.lifespan`'s, `fleet.admin.rotate_epoch`'s, and
   `0012_fleet_epoch.py`'s own docstrings).

   **Storage resolution must go through `app.dependency_overrides`, not
   a bare `get_storage()` call.** Every real end-to-end test that boots
   the app via a real `uvicorn` server (`tests/test_agent_commands
   _channel.py`, `tests/tls_support.py`) wires storage up purely through
   `app.dependency_overrides[get_storage]`, deliberately never setting
   `FLEET_DATABASE_URL` -- a bare `get_storage()` call in `lifespan`
   raised `RuntimeError` on every one of them, and since this rotation is
   *awaited directly before `yield`* (unlike `_alarm_check_loop`/
   `_backup_retention_loop`/`_log_retention_loop`, which are merely
   scheduled via `asyncio.create_task` and never awaited before `yield`,
   so a failure inside one of them is caught by that task's own loop, not
   by anything that could fail startup), the uncaught exception aborted
   `uvicorn` startup entirely (`SystemExit: 3` / `STARTUP_FAILURE`) on
   every one of those tests. **Fixed:** resolves storage via
   `app.dependency_overrides.get(get_storage, get_storage)()` -- mirrors
   exactly what `Depends(get_storage)` already does for every ordinary
   request.

   **A failed rotation aborts startup, uncaught -- main-session decision,
   round 2 of this same cross-review.** An intermediate version wrapped
   the whole rotation in `try/except Exception`, logged, not fatal
   (motivated by a second problem below) -- **rejected**: a silently
   failed rotation would mean the restore protection this whole package
   exists to provide is gone in production, without anyone noticing, the
   wrong trade to make for what turned out to be a test-fixture ordering
   problem, not a production concern at all. `lifespan` now raises
   straight out of a failed rotation, exactly like `NotifierConfigError`
   above.

   **The test-fixture ordering problem this used to work around, fixed on
   the test side instead:** `tests/test_agent_fetch_logs.py::
   fleet_base_url` starts its own real server from a **module-scoped**
   fixture, while the storage override used to be registered only by a
   **function-scoped**, `autouse` fixture -- pytest sets up higher-scoped
   fixtures before lower-scoped ones for a given test regardless of
   declaration order, so that module-scoped server's own `lifespan` used
   to run *before* anything was registered in `app.dependency_overrides`
   at all, and there was still no `FLEET_DATABASE_URL`. Reproduced
   directly: `test_fetch_logs_end_to_end_success` and two sibling tests
   failed with "Connection refused" (the server thread had already
   crashed at startup). **Fixed:** three new module-scoped fixtures in
   that file -- `_server_bootstrap_db_url` (a migrated, throwaway
   database), `_server_bootstrap_storage`, and
   `_override_storage_for_server_startup` (registers the override) --
   with `fleet_base_url` now taking `_override_storage_for_server_startup`
   as an explicit parameter, guaranteeing the override is in place before
   the server (and therefore this rotation) ever starts. The per-test,
   function-scoped `_override_storage` fixture is unchanged and still
   takes over for every individual test body as before; the bootstrap
   fixtures only ever matter for the one moment the shared server itself
   starts.

   **Tested:** `tests/test_fleet.py::
   test_lifespan_fails_loudly_when_the_epoch_rotation_cannot_resolve
   _storage` (no `FLEET_DATABASE_URL`, no override -- entering the
   lifespan raises `RuntimeError`) and `tests/test_fleet.py::
   test_lifespan_fails_loudly_when_rotate_epoch_itself_raises` (storage
   resolves fine, but a fake `Storage`'s own `rotate_epoch` raises --
   still propagates, not swallowed); `tests/test_fleet.py::
   test_lifespan_rotates_the_epoch_on_every_service_start` (two separate
   `TestClient(app)` lifespans against the same migrated database produce
   two different epochs) and `tests/test_fleet.py::
   test_lifespan_restart_an_old_bookmark_resumes_from_0_without_losing_a
   _pending_command` (a bookmark persisted against the pre-restart epoch,
   handed to a freshly started app via `Last-Event-ID`, resumes from `0`
   and still delivers the still-pending command, not skips it). The
   pre-existing real-server stop/restart acceptance test
   (`test_receive_commands_falls_back_to_polling_on_a_dropped_stream_and
   _resumes`) needed updating: since the restarted server now also
   rotates the epoch, the resumed stream correctly **redelivers** the
   still-pending first command before the genuinely new second one --
   updated to assert that redelivery explicitly, proving the harmless-
   redelivery claim directly rather than only asserting the end state.
   Both pre-existing `fleet.app.lifespan` tests
   (`test_lifespan_starts_and_cancels_the_alarm_background_task`,
   `test_lifespan_fails_loudly_on_a_misconfigured_alert_channel`) needed
   `upgrade(url)` added to their own setup -- they never migrated their
   database before (nothing they exercised touched storage synchronously
   before this package), and the automatic rotation now needs the
   `fleet_epoch` table to exist for its own `FLEET_DATABASE_URL`-based
   path to succeed.

**Verification** (fresh venv, `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"`): `ruff check .` and `mypy .` / `mypy protocol fleet
agent tools` all clean; `python -m pytest -W ignore::ResourceWarning -rA`
**2x**: **1286 passed, 1 skipped** (the pre-existing `age` CLI skip)
identical both runs, coverage **99%** (4897 statements, 25 missed)
identical both runs (statement count up from the original P5.1c
verification's 4889 by the new `_server_bootstrap_*`/`_override_storage
_for_server_startup` fixtures and the two new `test_lifespan_fails_loudly
_*` tests added by this round; missed-line count unchanged at 25, still
the same pre-existing, untouched gaps documented elsewhere in this file).

## P5.5a cross-review fixes: duplicate recipients, a memory DoS on the upload endpoint, an insufficient age-file check, and an unhandled handler exception

Cross-review of P5.5a (commit `167bc63`) confirmed the no-plaintext path,
the recipients source, the image mounts, the retention scheme, apartment
scoping, and security principle 3 -- even decrypting a test artifact with
the real `age` CLI. It found two required fixes and asked for two further
ones (main-session decision), all four addressed in this follow-up commit:

1. **Required -- duplicate recipients counted as two.** `agent.encryption
   .load_recipients` only checked `len(recipients) >= MIN_RECIPIENTS`
   after parsing -- the same public key listed twice on the boot-partition
   file satisfied that count without providing a genuine second,
   independent recipient, silently degrading "either key alone restores
   it" to a single point of failure. Fixed by deduplicating on each
   recipient's own **canonical string form** (`str(recipient)`, the
   normalized `age1...` encoding `pyrage.x25519.Recipient.__str__` always
   returns, comparing the *parsed* recipients rather than the raw input
   lines) before the count check, so a case-varied or otherwise
   differently-formatted duplicate of the same key cannot slip past
   either. Tested: two identical lines, two lines differing only in case,
   three lines naming only two distinct keys, and the positive case (a
   duplicate alongside two genuinely different recipients is accepted,
   still returning exactly the two distinct ones).

2. **Required -- a memory DoS on `POST /v1/backups`.** The endpoint used
   to `await request.body()`, buffering the *entire* declared body into
   memory before `MAX_BACKUP_UPLOAD_BYTES` was ever checked at all -- a
   caller that simply ignored the documented limit could exhaust memory
   regardless of what the eventual `413` said. Fixed two ways, together:
   a declared `Content-Length` above the cap is refused, `413`, before a
   single byte is read (a malformed `Content-Length` is not rejected for
   that alone -- the second check below still applies regardless); the
   body is streamed via `Request.stream()` straight into a temp file
   (`fleet.backup_storage.BackupBlobStorage.begin_upload`/
   `PendingBackupUpload`, new), hashed incrementally, with a running total
   checked on every chunk -- the moment it exceeds the cap, the upload is
   aborted (`413`), having held at most one chunk in memory and written at
   most the cap's worth to disk. The actual streaming loop is factored out
   into its own function, `fleet.app._stream_backup_body`, specifically so
   its "does it stop reading, not merely stop *accepting*, once the cap is
   exceeded" property is unit-testable directly against a synthetic async
   generator -- **verified empirically while building this fix that
   Starlette's own `TestClient` buffers a request body fully before an app
   ever sees it**, which would have made that property untestable through
   an HTTP call alone; an HTTP-level test (a generator body with no
   `Content-Length`, a monkeypatched small cap) additionally proves the
   real endpoint's own outer exception handling (abort-then-reraise) is
   wired correctly end to end. `BackupBlobStorage.store` (the
   non-streaming convenience method other tests and the retention job's
   fixtures use) is now itself implemented in terms of the same
   `begin_upload`/`PendingBackupUpload` primitives, not a second, separate
   write path.

3. **Fleet plausibility check for operational data, strengthened.**
   Checking only `AGE_HEADER_MAGIC` (`age-encryption.org/v1`) let
   `age-encryption.org/v1\n` followed by arbitrary plaintext through -- a
   buggy or malicious agent only had to prepend one fixed, public string.
   `fleet.app._looks_like_an_age_file` now also requires a syntactically
   plausible recipient stanza line (`-> ...`, the age format's own
   `Stanza` syntax) immediately after the header line -- still not a
   decrypt attempt (principle 3: the fleet has no private key to decrypt
   with even if it wanted to), but the uploaded bytes now have to actually
   look like the beginning of a real age file's *structure*. Reads only a
   bounded prefix (4096 bytes) back from the temp file for this check, not
   the whole body a second time. Tested: header line immediately followed
   by plaintext with no stanza line -> `422`; header magic with no
   newline at all -> `422`.

4. **Main-session decision -- an unexpected exception inside a command
   handler must never crash the agent.** Two related but independent
   gaps, both closed:
   - `agent.loop.execute_command`'s own `handler(command, ctx)` call had
     no `try`/`except` around it at all -- any handler raising anything
     uncaught propagated straight out of `execute_command`, out of
     `run`'s own `for item in commands:` loop, crashing the whole agent
     process (every command after the offending one would then never
     execute either, exactly the failure mode section 7's "the agent
     keeps running" already rules out for a rejected/expired command).
     Fixed with a generic `except Exception` around the dispatch itself,
     turning any handler's unexpected exception into a failed
     `CommandResult` (`repr(error)` in `error_text`, the same
     control-character-escaping reasoning `_append_local_log` already
     documents for a cloud-echoed rejection reason) -- a safety net
     underneath every handler, not just `backup_now`'s own.
   - `_handle_backup_now`'s own per-kind `except` clause named four
     specific exception types (`RecipientsError`/`OSError`/`sqlite3
     .Error`/`ValueError`) -- `tarfile.TarError` (e.g. a corrupted
     intermediate tar) is none of those, and used to propagate past this
     handler's own boundary too. Widened to `except Exception`;
     `create_backup`'s own internal plaintext cleanup (the sqlite
     snapshot, the tar) is unaffected, since it already runs via its own
     unconditional `finally` regardless of which exception type
     propagates out of it. Tested by injecting a real `tarfile.TarError`
     from inside `create_backup`'s own `tarfile.open(...)` call -- the
     device-configuration half of `backup_now` still succeeds
     independently, the combined result is reported as a failure, and no
     plaintext is left behind under the staging directory.

**Verification** (fresh venv, `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"`): `ruff check .` -- `All checks passed!`; `mypy .` --
`Success: no issues found in 93 source files`; `mypy protocol fleet agent
tools` -- `Success: no issues found in 51 source files`; `python -m tools
.check_image_config` -- `Image configuration plausible`; `python -m
pytest -W ignore::ResourceWarning -rA` **2x**, both exit code 0, **1149
passed, 1 skipped** each run (the one skip: no `age` CLI binary on this
machine), coverage **99%** both runs (4492 statements; run 1: 14 missed;
run 2: 13 missed) -- the one-line difference is `fleet/storage.py`'s own
pre-existing `remove_device` concurrent-race branch, the same
timing-based coverage wobble already documented in this file's P5.1b
cross-review entry, unrelated to this fix; every remaining miss in both
runs is pre-existing and unrelated (`agent.loop`'s own still-open
`collect_heartbeat`/`send_heartbeat`/`reconcile_desired_state`
placeholders, `fleet/admin.py`'s own pre-existing gap, `tools
/check_image_config.py`'s own pre-existing CLI-entry-point gaps). In
`watchdog/`: `go vet ./...` clean, `go test ./...` -- all three packages
`ok`, `bash check_contract.sh` -- "Contract test passed" (run with the
`agent` extra installed on `PATH`; `watchdog/`'s own `go.mod` untouched).

## P5.5a -- end-to-end encrypted backups to two recipients (sections 15.1, 15.2, 15.3)

**`backup_now` is genuinely executed, both kinds of backup real.**
`agent/loop.py::create_backup` builds and stages:

- **Device configuration** (`operational_data=False`): plain JSON,
  containing exactly `apartment_id`, `agent_version`, `created_at`, and --
  when the watchdog's own state file is present and readable -- the
  agent's own self-swap `agent_digest`/`agent_proven_digest`
  (`_build_device_config_snapshot`). Nothing else: section 15.1's own
  table also lists broker settings, WireGuard peer, and timezone, but this
  scaffold has not built any of the features that would make those true
  yet (section 14's WireGuard tunnel, a Mosquitto broker configuration,
  P5.4's full desired-state reconciliation) -- adding a fabricated value
  instead of omitting the key would have been exactly the "invented
  stopgap" this codebase's placeholders elsewhere already refuse to
  produce. Contains no tenant data, pinned by a planted-marker test.
- **Operational data** (`operational_data=True`): thermoctl's SQLite
  database (a consistent snapshot via `sqlite3.Connection.backup`'s online
  backup API, opened read-only, never a raw file copy) plus Zigbee2MQTT's
  `database.db`/`coordinator_backup.json` if present, bundled as a tar and
  **encrypted before it ever touches an upload buffer** -- real `age`
  format (`pyrage`, Python bindings to the Rust `age`/`rage`
  implementation; chosen over hand-rolling the format or its cryptography,
  per CLAUDE.md's "no invented functionality"), to **two recipients
  always** (the landlord's everyday key and one offline key, project owner
  decision 2026-09-26/27) -- `age -d -i <your-key-file> ...` decrypts it
  with either. Every intermediate plaintext file (the sqlite snapshot, the
  plaintext tar) lives under a staging directory, mode `0600`
  (`tempfile.mkstemp`'s own default), and is removed unconditionally --
  including on every error path -- so no plaintext operational data ever
  survives a call, success or failure. Streamed both ways (hashing,
  encryption, upload) rather than materialized as one in-memory `bytes`
  object.

**`agent/encryption.py`** (new, small, reusable module -- explicitly built
this way so P5.3b's future encrypted diagnostic bundle can call it
directly instead of duplicating recipients-file handling): `load_recipients`
reads the **boot-partition** recipients file
(`/boot/firmware/thermoctl/backup-recipients.txt`, one age recipient per
line, `#`-comments allowed) via `agent.safe_io.read_text_safe` (the same
symlink/non-regular-file hardening `agent.loop`'s own state files already
get) and validates every line as a real X25519 age recipient
(`pyrage.x25519.Recipient.from_str`) -- **fails closed**: missing, unsafe,
containing an invalid line, or fewer than two valid recipients all raise
`RecipientsError` and refuse the whole backup, never a plaintext fallback.
The public keys are **written locally onto the boot partition when the
image is prepared and never taken from the cloud** -- the same "hard-coded
on the device, not cloud-supplied" reasoning CLAUDE.md's principle 2
already applies to the image source list; a compromised cloud therefore
cannot swap in its own recipient and read operational data as it arrives.
`encrypt_stream` is a thin, single call-site wrapper around
`pyrage.encrypt_io` (streams both ends).

**Upload -- new fleet endpoint, `POST /v1/backups`** (`fleet/app.py`):
agent-token-authenticated exactly like every other `/v1/...` endpoint with
no apartment in its own address (`require_apartment_token_by_hash`);
`kind`/`content_hash` as query parameters, the raw bytes as the body (no
JSON wrapping around a multi-megabyte blob). Checked, in order: size
against `protocol.backups.MAX_BACKUP_UPLOAD_BYTES` (against the actually-
received body, never a declared `Content-Length` alone); `content_hash`
matches a fresh SHA-256 of the received body; for `operational_data`, the
body must start with the real age format's own header line
(`age-encryption.org/v1`) -- refused, `422`, before a single byte reaches
disk, so a buggy or compromised agent cannot store plaintext tenant data
by accident (this is a structural plausibility check on the file's own
framing, not a decrypt attempt -- the fleet holds no private key to
decrypt with in the first place, principle 3); for `device_config`, the
body must parse as JSON. The fleet **never otherwise parses** an
operational-data upload.

**Storage**: `fleet/backup_storage.py` (new) -- blobs on the filesystem
under a configurable directory (`FLEET_BACKUP_STORAGE_DIR`, not the
database), one subdirectory per apartment and kind, a fresh random id per
blob (never named after the caller-supplied, unverified-until-just-now
`content_hash`), written atomically (`O_EXCL` temp file, then
`os.replace`). Metadata (`backup_id`, `apartment_id`, `kind`, `created_at`,
`size_bytes`, `content_hash`, `storage_path`) in a new `backups` table
(`fleet/migrations/versions/0011_backups.py` -- originally `0010`, chained
onto `main`'s `0009_commands.py`; re-chained at merge time onto P5.3a's own
parallel `0010_command_log_excerpts.py`, main-session convention) --
mirrors `commands`'s own "wire id is a separate, random, unique-indexed
column, never the primary key" reasoning.

**Retention (section 15.2): "the last 14 daily backups, plus one weekly
backup for each of the last eight weeks", applied per apartment and per
kind.** `fleet/backup_retention.py` (new) -- a classic
grandfather-father-son rotation: group by UTC calendar date, keep the
newest of each of the 14 most recent dates; group the *remaining* backups
(deliberately excluding every ISO week already represented by a
daily-kept backup -- without that exclusion, a daily upload rhythm would
always have its two or three most recent weeks entirely covered by the
daily rule already, silently spending several of the 8 weekly slots on
weeks that add no additional retained backup and shrinking the real
retention horizon well under "8 weeks") by ISO calendar week, keep the
newest of each of the 8 most recent remaining weeks. A pure function
(`select_backups_to_keep`, clock-injected, tested directly with
synthetic data spanning 90 days: exactly 22 kept, the rest dropped) plus a
real I/O wrapper (`run_backup_retention`) run periodically from
`fleet/app.py`'s own lifespan (mirrors `_alarm_check_loop`'s existing
shape, default hourly, `FLEET_BACKUP_RETENTION_INTERVAL_S`).

**UI** ("Eine Wohnung", `fleet/ui_apartment.py`/`fleet/ui_routes.py`): a
"Sicherungen" section lists every backup (kind, time, size, SHA-256 hash),
each with a download link (`GET /ui/apartments/{id}/backups/{backup_id}
/download`, behind the P3.0 login like every other `/ui` route, scoped to
the apartment -- another apartment's backup id is a 404, indistinguishable
from an unknown one) and, for operational data only, the ready-made
`age -d -i <your-key-file> -o backup.tar <file>` command right there
(project owner: "a cumbersome path leads to weakening the filter
instead").

**Image (`image/common/`)**: `agent-compose.yml` gains three new
**read-only** bind mounts -- the boot-partition recipients file's own
directory, thermoctl's data directory (`/var/lib/thermoctl`, this
repository's own chosen convention pending a real thermoctl/Zigbee2MQTT
compose file, P5.4/P5.6), and Zigbee2MQTT's data directory
(`/var/lib/zigbee2mqtt`) -- all justified inline in the compose file's own
comments. `tools/check_image_config.py::check_agent_compose_file` asserts
all three lines are present. `image/common/README.md` documents the
recipients file's path/format and the section 19.5 preparation-tool step
that writes it (**not built here** -- still a documented TODO, same
status as the other still-unimplemented image-build steps this file
already tracks).

**Protocol**: `protocol/backups.py` (new) -- `BackupKind` (closed,
`device_config`/`operational_data`), `BackupUploadAccepted`,
`MAX_BACKUP_UPLOAD_BYTES`, `AGE_HEADER_MAGIC`. `PROTOCOL_VERSION` bumped
originally 3 -> 4; at merge time (this package's own change had landed in
parallel with, and numbered the same as, P5.3a's own `LogExcerpt`
addition) re-numbered to **5** -- a wholly new module counts as a change
to "the models", per that file's own established reading of section 18.2.

**Constraints honoured**: `protocol.commands.CommandType` unchanged (no
new command was needed -- `backup_now` already existed); `watchdog/`
untouched (`go vet`/`go test`/`check_contract.sh` all still pass,
including with the new `agent` extra's `pyrage` dependency installed --
the watchdog itself has no dependency on it, its own `go.mod` is
untouched); no private key anywhere in the repo, the image, the agent, or
the fleet -- every identity generated in this package's own tests is
freshly generated at test runtime (`pyrage.x25519.Identity.generate()`),
never committed.

**Open**: **restore (section 15.2/15.3 step 4) is P5.5b, not this
package** -- "the agent fetches the device configuration and, on a swap,
the encrypted operational data; the landlord enters the decryption key
once in the fleet UI, only passed through, never stored" has no stub at
all yet, fleet-side or agent-side. `docs/implementation_plan.md` splits
P5.5 into P5.5a (done) / P5.5b (open) accordingly.

**Tests** (`tests/test_agent_encryption.py`, `tests/test_agent_backup.py`,
`tests/test_agent_backup_e2e.py`, `tests/test_fleet_backups.py`,
`tests/test_backup_storage.py`, `tests/test_backup_retention.py`,
`tests/test_storage_backups.py`, `tests/test_ui_backups.py`; plus small
updates to `tests/test_agent_loop_execution.py` and `tests/
test_watchdog_contract.py`, see below) -- real cryptography throughout,
never a mock of `pyrage`/age: two freshly generated X25519 identities,
each decrypts alone (via `pyrage` directly; the real `age` CLI too, when
available -- skipped with a stated reason on this development machine,
which has no `age` binary installed); fewer than two recipients, an
invalid recipient line, a missing recipients file, and a symlinked/FIFO
recipients file are each refused, with a marker string planted in a fake
thermoctl database and Zigbee2MQTT files asserted absent from every file
under the staging directory in every one of those cases (and from every
captured request body in the upload-level tests); the fleet endpoint
rejects a non-age operational-data upload and a non-JSON device-config
upload, a content-hash mismatch, an oversized body, and an unauthenticated/
wrong-token request; the device-configuration backup's own content is
pinned marker-free; retention is proven to keep exactly 14 daily + 8
weekly (90 days of synthetic data, non-overlapping construction) and to be
idempotent; the UI download route requires login and is 404 for another
apartment's backup id; and one true end-to-end test drives `backup_now`
through `agent.loop.run` against the real `fleet.app.app` over real TLS,
with a fake thermoctl SQLite database and a fake Zigbee2MQTT directory
standing in for the real services, asserting both backup rows exist in
the fleet's database afterward and that the stored operational-data blob
decrypts, with either generated identity, to a tar containing all three
expected files.

**Two small, adjacent test fixes, both narrowing an over-broad assertion
that this package's own legitimate new code would otherwise have
violated, not weakening what either test actually protects:**

- `tests/test_watchdog_contract.py::test_file_is_not_json` used to assert
  "`agent.loop` does not import `json` anywhere" as a proxy for "the
  watchdog-contract files are line-based, not JSON" -- true only by
  coincidence before this package, since nothing in that module needed
  `json` for anything. The device-configuration backup's content
  legitimately is JSON (section 15.1: "device config as JSON"), a
  different file, never read by the watchdog. Narrowed to check the three
  watchdog-contract writer functions' own source directly
  (`inspect.getsource`, asserting none of them contains `"json."`) instead
  of the whole module's import list.
- `tests/test_agent_loop_execution.py`'s parametrized "not yet available,
  honest failure" test used to include `backup_now` (P5.5's own
  placeholder message) -- removed from that parametrization (`backup_now`
  is no longer a placeholder) and replaced with a dedicated test for the
  one honest-failure case that remains: `backup_now` refused, cleanly,
  when `ExecutionContext.backup_config` is `None` (the CLI's own
  `--apartment-id` etc. were not given).

**Verification** (fresh venv, `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"`): `ruff check .` -- `All checks passed!`; `mypy .` --
`Success: no issues found in 93 source files`; `mypy protocol fleet agent
tools` -- `Success: no issues found in 51 source files`; `python -m
tools.check_image_config` -- `Image configuration plausible`; `python -m
pytest -W ignore::ResourceWarning -rA` **2x**, both exit code 0, **1134
passed, 1 skipped** each run (the one skip: no `age` CLI binary on this
machine), coverage **99%** (4425 statements, 13 missed) identical across
both runs -- every one of the 13 remaining misses is pre-existing and
unrelated to this package (`agent.loop`'s own still-open `collect_heartbeat`/
`send_heartbeat`/`reconcile_desired_state` placeholders, `fleet/admin.py`'s
own pre-existing gap, `tools/check_image_config.py`'s own pre-existing
CLI-entry-point gaps). In `watchdog/`: `go vet ./...` clean, `go test
./...` -- all three packages `ok`, `bash check_contract.sh` --
"Contract test passed" (run with the `agent` extra, including `pyrage`,
installed -- the watchdog's own `go.mod` has and needs no new dependency;
this only reflects the Python side of the cross-language contract test
now also depending on the new extra, exactly like the prior commit's own
"install the agent extra for the contract test" CI fix already
anticipated).

## Cross-review fix: single-key enforcement + non-permissive apt pinning (image/, section 19)

Two supply-chain gaps found reading back the Docker-apt-repository fix
below, fixed in the same worktree/branch:

1. **`fetch-docker-key.sh` checked only the first fingerprint in the
   download.** A downloaded file can validly contain more than one
   concatenated OpenPGP key; the script compared only the first key's
   fingerprint but then installed the **entire raw file** as the keyring,
   so a genuine key plus a smuggled-in extra key would both end up trusted
   for the repository. Fixed: the script now requires **exactly one**
   primary key (`pub:` count == 1, via `gpg --with-colons
   --import-options show-only`) and only then compares that one key's own
   fingerprint -- refusing (fail-closed) on zero keys, more than one key, an
   empty download, a garbled download, or a missing `curl`/`gpg`.
   `DOCKER_KEY_URL`/`DOCKER_KEYRING_PATH`/`DOCKER_KEY_FINGERPRINT` are now
   overridable via environment variables *for tests only* (production
   image builds never set them). New `tests/test_fetch_docker_key.py`
   exercises the real script with real throwaway ed25519 keys generated by
   a real `gpg` in an isolated per-test `GNUPGHOME` (skipped with a reason
   if `gpg`/`curl`/`bash` are not on `PATH`): correct single key installs,
   a file with the real key plus an extra key is refused with nothing
   installed, a different key is refused, and an empty/garbled download is
   refused. `tools/check_image_config.py::check_docker_key_fetch` also
   asserts the script's source still contains the primary-key-count
   enforcement, so a regression can't silently drop it.
2. **The apt pin for "everything else from `download.docker.com`" was
   `Pin-Priority: 1`**, not permissive enough to *install by default*, but
   still enough to let apt install a package that exists **only** at that
   origin if anything ever requested it -- exactly what `docker-ce`'s own
   `Recommends: docker-ce-rootless-extras, docker-buildx-plugin` would
   trigger. **Reproduced for real** in the local E2E VM: after the first
   fix's provisioning run (still on `Pin-Priority: 1`, without
   `--no-install-recommends`), `apt-get install docker-ce ...` had in fact
   pulled in `docker-buildx-plugin` and `docker-ce-rootless-extras` as
   automatically-installed dependencies -- confirmed with `dpkg -s`
   showing both `install ok installed` before this fix, removed via
   `apt-get autoremove` to re-test cleanly. Fixed with two independent
   lines of defense: `preferences.d/docker`'s wildcard stanza is now
   `Pin-Priority: -1` (apt's own "never installed", not merely
   deprioritized), and the documented build step (and
   `tools/e2e/provision/install-packages.sh`) now installs the four named
   packages with `apt-get install --no-install-recommends`.
   `tools/check_image_config.py::check_docker_apt_preferences` now parses
   the actual `Pin-Priority:` values (not a substring match, since `"1"` is
   a substring of `"-1"`/`"600"`) and asserts `-1` and `600` are both
   present and that a bare `1` is not.

**E2E re-verification** (existing VM, `docker-ce`/`docker-ce-cli`/
`containerd.io`/`docker-compose-plugin` removed and `apt-get autoremove`d
first, apt/keyring files removed, then re-provisioned from the fixed
`tools/e2e/provision/install-packages.sh`):
`apt-cache policy docker-buildx-plugin docker-ce-rootless-extras` shows
`Installed: (none)` / `Candidate: (none)`, every version pinned `-1` from
`download.docker.com`; the four Docker-repo packages show `Status: install
ok installed` via `dpkg -s`; `apt-cache policy docker-ce` still shows `600`
from `download.docker.com`. All six scenarios (a-f) re-run and **PASS**
(one retry needed for scenario (f) alone: a stale
`/var/lib/thermoctl-agent/last_event_id` file left over from an earlier
fleet-database reset during this same fix round caused the SSE resume
cursor to point past the one command just created, hanging the receive
loop indefinitely -- a manual-reprovisioning artifact of this fix round,
not a bug in the fix, in `tools/e2e/`, or in `agent`/`fleet` code; clearing
that one file and re-running scenario (f) alone passed immediately).

Full verification (same fresh venv): `ruff check .` clean, `python -m
tools.check_image_config` plausible, `pytest tests/test_image_config.py
tests/test_fetch_docker_key.py` 39 passed, full suite 997 passed (99%
coverage). No change to `fleet/`, `agent/`, `protocol/`, `watchdog/` code.
VM stopped (not deleted) afterward; total on-host footprint for the whole
VM directory measured at 3.4 GB (`du -sh ~/.lima/thermoctl-e2e-basestation`),
comfortably inside the ~5 GB budget; only `thermoctl-e2e-*` resources
touched, no global prune.

## Fix: Docker and Compose v2 from Docker's official apt repository (image/, section 19)

**Finding (P5.E, 2026-09-27):** `image/common/packages.txt` named
`docker-compose-v2`, which does not exist as a Debian 13 "trixie" package
(`E: Unable to locate package docker-compose-v2`), and Debian's own
`docker-compose` package only ever installs the legacy hyphenated v1
script, never the `docker compose` (space) v2 CLI subcommand
`watchdog/runtime.go` actually invokes (`execCommand(r.bin, "compose",
...)`). On a real device the watchdog could never start the agent.

**Decision by the project owner (2026-09-27):** install `docker-ce`,
`docker-ce-cli`, `containerd.io`, `docker-compose-plugin` from Docker's
official apt repository (`download.docker.com`), with security updates
continuing to come in via apt like everything else in this image, rather
than a static binary download or a third-party workaround confined to a
test environment. `docker-buildx-plugin` deliberately **not** installed --
the base station never builds an image, only preloads the already-built
agent image (section 19.3).

**What was built** (`image/common/apt/`): a deb822 repository definition
(`docker.sources`, `Suites: trixie` for both `image/pi/` and `image/x86/`,
`Signed-By:` a keyring file rather than a system-wide `apt-key add`), an
apt preferences file (`preferences.d/docker`) pinning **only** `docker-ce`,
`docker-ce-cli`, `containerd.io`, `docker-compose-plugin` to that
repository (origin-wide priority 1, these four names at priority 600 --
apt pinning so no other package can come from `download.docker.com`), and
a fetch script (`fetch-docker-key.sh`) that downloads Docker's signing key
over HTTPS and **verifies its fingerprint before installing it**, refusing
(non-zero exit) on any mismatch. The pinned fingerprint,
**`9DC858229FC7DD38854AE2D88D81803C0EBFCD88`**, is Docker's published
release key (verified in the local E2E run below by re-deriving it from
the actually-installed keyring with `gpg --show-keys`, not just trusted
from the script's own comment). `tools/check_image_config.py` gained four
new checks (`check_docker_packages_from_official_repo`,
`check_docker_apt_source`, `check_docker_apt_preferences`,
`check_docker_key_fetch`), each with passing and failing cases in
`tests/test_image_config.py`. `image/common/README.md`,
`image/pi/README.md`, `image/x86/README.md` now document the exact build
step order (`ca-certificates` first, then the key fetch, then the
repo/pinning files, then `apt-get update`, then the rest of
`packages.txt`), and that the `DOCKER_GID` `.env` step (P5.7 hot-fix round
2) must run *after* this whole sequence, since `docker-ce` is what creates
the `docker` group in the first place.

**E2E re-run:** `tools/e2e/provision/install-packages.sh` now provisions
the VM from these exact shipped repo files (ca-certificates/curl/gnupg,
`fetch-docker-key.sh`, `docker.sources` + `preferences.d/docker`, a second
`apt-get update`, then the rest of `packages.txt`) instead of the previous
static-binary workaround; `tools/e2e/provision/install-compose-plugin.sh`
was removed. Re-run against the existing `thermoctl-e2e-basestation` VM
(`limactl start`, the stale static compose-plugin binary and the old
`docker.io`/`docker-compose` packages removed first so the new apt-based
path was genuinely exercised, not masked by leftovers): `docker-ce`,
`docker-ce-cli`, `containerd.io`, `docker-compose-plugin` all installed
from `download.docker.com` (`apt-cache policy docker-ce` shows `600
https://download.docker.com/linux/debian trixie/stable`), `docker compose
version` reports `v5.5.1`, and the installed keyring's fingerprint
(`gpg --show-keys /etc/apt/keyrings/docker.asc`) matches
`9DC858229FC7DD38854AE2D88D81803C0EBFCD88` exactly.

All six scenarios then ran against the real code on this branch and
**PASSED** (one re-run of scenario (a) needed after clearing stale device
state left over from a previous P5.E session's fleet database -- not a
finding about this fix, the fleet DB simply isn't reset by `limactl
start`):

| Scenario | Result |
|---|---|
| (a) registration end-to-end | **PASS** |
| (b) mounts and permissions | **PASS** |
| (c) watchdog swap | **PASS** |
| (d) watchdog rollback (restart-count and the real 10-minute deadline) | **PASS (both)** |
| (e) no registry pull | **PASS** |
| (f) command channel | **PASS** |

No change to `fleet/`, `agent/`, `protocol/`, or `watchdog/` code. Full
verification (fresh venv, `python3.13 -m venv` + `pip install -e
".[dev,fleet,agent]"`): `ruff check .` clean, `python -m
tools.check_image_config` plausible, `pytest
tests/test_image_config.py` 32 passed, full suite 990 passed (99% overall
coverage, `tools/check_image_config.py` itself 92%). VM stopped (not
deleted) afterward; host disk usage stayed inside the ~5 GB budget (`df -h
~`: 31 GB free before, 29 GB free after).

**P5.E discrepancy list (`tools/e2e/README.md`) updated:** item 1
(`docker-compose-v2` does not exist in Debian 13 "trixie") marked
**RESOLVED**, pointing back here. Items 2-5 (hardware watchdog unit not
enableable under QEMU/vz, host-side uid/gid 10002 not literally required,
`/var/lib/thermoctl-agent` ownership/mode not documented, no template for
the watchdog's build-time state file) are unrelated to this fix and remain
open, as recorded there.

## P5.3a -- `fetch_logs` with an on-device allowlist filter (sections 6, 7, 21.5)

Built per the project owner's 2026-09-27 decision (recorded in `docs/specification.md`
21.5's own "Decided afterward" paragraph): `fetch_logs` now has a real agent-side handler,
not the P5.2 placeholder.

**Allowlist, not denylist** (`agent/log_filter.py`): a line only ever leaves the device if
(1) it structurally matches thermoctl's own text log line shape
(`thermoctl/logging.py::TextFormatter`, read from the sibling repository) **and** (2) its
message (and, for `WARNING`/`ERROR`/`CRITICAL`, its `extra=` tail) matches an explicit,
per-level table of known thermoctl message templates.

**Cross-review correction (this section rewritten after a first, incorrect version):** the
first version of this filter let *any* `WARNING`/`ERROR`/`CRITICAL` line through unmasked,
reasoning that severity alone implied safety. Cross-review reproduced real leaks this let
through unchanged -- `thermoctl/integrations/notification.py::_attempt_delivery` logs every
fault notice's title/text at `WARNING` unconditionally (e.g. "Sensorstörung in Kinderzimmer
Mia"), several call sites carry a device's display name in an `extra={"geraet": ...}` field
(`domain/controller_channels.py`, `services/device_commands.py`, `services/publishing.py`),
and `domain/legacy_system.py` logs a raw, unvalidated sensor payload under `extra={"wert":
...}`. **Fixed:** every level now goes through the same explicit template table
(`_WARN_FIXED_MESSAGES`/`_WARN_TEMPLATES` for `WARNING`+, four fixed messages for `INFO`) --
each template names exactly which of its own parameters are safe to keep (a numeric id, a
closed enum value) and which must be replaced by a fixed placeholder (a zone/device name, an
exception's rendered text). A `WARNING`+ line matching **no** template is not silently
dropped -- it is *reduced* to `<timestamp> <LEVEL> <logger>: <nicht freigegebene Meldung>`
(message and `extra` tail both discarded) and still counted in `dropped`, so a missing
template shows up as "some lines carried no approved content" rather than a silent gap.
`INFO` keeps the narrower original rule: an unrecognised shape is dropped outright, not
reduced. The `extra=` tail itself is parsed independently (`_mask_extra_tail`): a small
allowlist of keys with a known-safe value shape is kept verbatim (`schluessel`, `zone_id`,
`host`/`port`/`bind`, `wartezeit_s`/`verbindungsdauer_s`, ...), a fixed list of
always-name-or-value-shaped keys (`geraet`, `zone_name`, `device_name`, `wert`, `value`,
`messwert`, `grund`, `fehler`, `client_id`, ...) is always replaced by `<wert>`, and any
other, unrecognised key is dropped from the tail entirely rather than shown either raw or
masked.

**Second cross-review correction: `topic` needs its own check, not a shape regex.** The
first fix above still let `topic` through verbatim whenever its value looked path-like and
space-free (`^[\w/.\-:]+$`). Reproduced: thermoctl's own Zigbee2MQTT actuator topics
(`integrations/actuators.py::Zigbee2MqttValve`/`ThermostatValve.__init__`,
`services/publishing.py`'s own publish calls) are built as `f"{base}/{device_name}/set"`,
where `device_name` is the Zigbee2MQTT **friendly name** -- Z2M's own convention uses `_`/`-`
instead of spaces, so `zigbee2mqtt/Kinderzimmer_Mia/set` is exactly as path-like and
space-free as a real id-based topic, and passed through unchanged (e.g. via
`integrations/mqtt/client.py::run`'s own `extra={"topic": topic}` on a disallowed switching
attempt). **Fixed** (`_mask_topic`): `topic` is now kept verbatim **only** if it fully
matches one of two explicit, real thermoctl control-topic shapes --
`<prefix>/zones/<digits>/command/<kind>[/<key>]` (`integrations/mqtt/commands.py::_PATTERN`)
or `heizung/thermostate/<digits>/<attribute>/get` (`domain/legacy_system.py`'s own fixed
shape) -- every other topic, including every Zigbee2MQTT device/actuator topic, becomes
`<wert>`.

**Third cross-review correction: the leading segment of a command topic was still kept
verbatim.** The second fix's `<prefix>/zones/<digits>/command/<kind>[/<key>]` pattern
checked the *shape* of the leading segment (`[^/\s]+`, no slash or whitespace) but not its
*content* -- `integrations/mqtt/commands.py::split_topic` additionally requires that segment
to equal the deployment's own configured `mqtt_prefix` exactly, a check this agent-side
filter cannot perform (it does not know the prefix, and must not guess it from what it
sees). On a shared local MQTT broker, any other publisher can put an arbitrary name there,
and `thermoctl/app.py`'s own "Unbrauchbarer Befehl verworfen"/"Befehl für unbekannte Zone
verworfen" log lines fire *precisely* for such a foreign topic -- reproduced with
`topic=Kinderzimmer-Mia/zones/999/command/boost` and
`topic=AnnaMustermann/zones/7/command/boost`, both kept unchanged by the second fix. **Fixed
for real** (`_mask_zone_command_topic`): the leading segment is now **never** kept, under
any circumstances -- only `zone` (numeric), `kind` (verified against the exact closed set
`split_topic` itself accepts: `setpoint`/`operating_mode`/`boost`/`cancel_override`/`mode`/
`parameter`), and, where applicable, `key` (a digit-only mode id for `mode`, or one of the
15 real control parameter names from `domain/zone_settings.py::PARAMETERS` for
`parameter` -- never a bare shape match like `split_topic`'s own `[a-z][a-z0-9_]*`
validation, which a room or device name could satisfy just as well) are ever kept, each
independently verified against thermoctl's real, closed vocabulary; anything that does not
check out, including an unrecognised `kind` (e.g. a Zigbee2MQTT name crafted to end in
`/command/set/set`), collapses the **whole** topic to `<wert>`, not just its prefix. The
same "verify the actual vocabulary, not just the shape" correction was applied to the
legacy-system pattern's `<attribute>` (`_LEGACY_ATTRIBUTES`, restricted to
`_NUMBER_ATTRIBUTE`/`_TEXT_ATTRIBUTE`'s own 7 names) even though that pattern's other
segments are all fixed literals with no equivalent spoofing risk.

**Coverage gap closed, not a leak**: `integrations/mqtt/client.py::run` logs two of its own
messages ("MQTT-Verbindung verloren; neuer Versuch folgt", "MQTT-Nachricht konnte nicht
verarbeitet werden") through a `melden = log.exception if short_lived == 0 else log.error`
alias -- the alias only decides the level (both branches log the same message text), so one
allowlist entry per message covers both branches; a third message from the same function
("MQTT-Verbindung bricht sofort wieder ab...") was added alongside it while re-reading that
module. A repository-wide grep for other alias-style log calls (`= log\.(warning|error
|exception|critical|info)\b`) found no other instance.

**This table is a snapshot of one thermoctl version, not a permanent contract** -- a future
thermoctl release that adds or rewords a log call does not fall through to being logged in
full; it simply stops matching any template and becomes `<nicht freigegebene Meldung>` until
this table is updated. This is the allowlist working as intended (fails visibly, by
omission, never silently by leaking), but it means **`agent/log_filter.py`'s template table
must be revisited with every thermoctl upgrade** -- a stale table does not leak, it quietly
loses signal instead, which is the failure mode to watch for.

**Placeholders, within an already-allowed template's declared-unsafe parameters:**
temperatures/setpoints (`21,5 °C`) -> `<temperatur>`/`<sollwert>`; a zone/device display name
-> `<name>`; an exception's rendered text, a raw sensor/config payload value, or a display
name in the `extra` tail -> `<wert>`. Every placeholder is fixed and dumb -- never a stable
hash of the underlying value, so two lines carrying the same reading cannot be correlated
across the log even after masking (verified by
`tests/test_log_filter.py::test_placeholders_are_identical_across_different_values_no_correlation`).

**Agent** (`agent/loop.py`): `_handle_fetch_logs` reads the last `Command.lines` lines of
the `thermoctl` container's log via the local Docker Engine API over the Unix socket
(`read_container_log_lines`, own hand-rolled demultiplexer for Docker's multiplexed log
stream framing -- **no Docker SDK dependency**, mirroring CLAUDE.md security principle 6's
reasoning for the watchdog, applied here for the same "no dependency this package does not
need" reason), filters through `agent.log_filter.filter_log_lines`, and uploads the result
as `protocol.commands.LogExcerpt` via `POST /v1/commands/{id}/logs`. Every failure path
(no client configured, the log source raising, the upload being refused) is an honest
failed `CommandResult`, never a fabricated success -- matching this module's own existing
"never an invented stopgap" rule.

**Protocol**: `protocol.commands.LogExcerpt` (`command_id`, `lines` -- already filtered on
the device, documented as such --, `dropped_lines`, `source`, `captured_at`), capped at
`MAX_LOG_EXCERPT_LINES` (500, reusing `Command.lines`'s own bound) and a per-line length
cap. `PROTOCOL_VERSION` bumped to 4 (a wholly new model counts as a model change, per the
project owner's own literal reading of "any change to the models" from P5.1/18.2).

**Fleet**: `POST /v1/commands/{id}/logs` (`fleet/app.py::receive_log_excerpt`, agent token,
`require_apartment_token_by_hash`) -- the command named must belong to the authenticated
apartment and be a `fetch_logs` command (otherwise 404, deliberately indistinguishable from
"unknown", mirroring `receive_command_result`'s own reasoning), one excerpt per command
(a second upload is 409), and a cheap aggregate-size backstop (413) independent of the
per-field model bounds -- **the fleet does no filtering of its own**, only this size cap.
Storage: `command_log_excerpts` table (`fleet/migrations/versions/0010_command_log_excerpts.py`,
`fleet.storage.Storage.store_log_excerpt`/`get_log_excerpt_for_command`).

**Retention** (project owner condition 4): `Storage.delete_expired_log_excerpts`, run
periodically by a background loop in `fleet/app.py::lifespan` (`_log_retention_loop`, the
same "thin scheduling wrapper, tested via an injected clock on the logic it calls" pattern
`_alarm_check_loop` already established) -- 14 days by default, both the retention window
(`FLEET_LOG_RETENTION_DAYS`) and the check interval (`FLEET_LOG_RETENTION_CHECK_INTERVAL_S`)
env-configurable, per CLAUDE.md ("nothing hard-coded").

**UI**: "Eine Wohnung"'s "Befehle" history shows a `fetch_logs` row's stored excerpt
alongside it -- capture time, source, the dropped-line count shown plainly ("N Zeilen
entfernt"), and the filtered lines themselves in a monospace, escaped block
(`fleet/ui_apartment.py::LogExcerptDisplay`, `fleet/templates/ui/apartment.html`).

**Tests**: `tests/test_log_filter.py` -- one fixture line per real thermoctl call site (not
a synthetic composite), built the way `TextFormatter` would actually emit it: every
`fault_notice`/`problem_report` title+text shape (zone/device name masked, in both the
`WARNING`-level entry and the all-clear), the tenant-report shape (name masked in both the
title and the body's first line; every later line of the tenant's own free text dropped
outright, having no log-format prefix at all), the `extra`-tail leaks cross-review
reproduced (`geraet`/`wert`/`grund` masked, `zone_id`/`topic`/`schluessel` kept), the two
`app.py` call sites that embed an exception's text directly in the message (always masked),
the migration-lock env-var warning, the legacy-data count/index warnings (kept verbatim), an
unrecognised `WARNING` line (reduced to the fixed placeholder and counted), an unrecognised
`INFO` line (dropped outright), and the placeholder-identity/no-correlation test. `tests/test_agent_fetch_logs.py` (the
Docker log-stream demultiplexer; the handler against a stub log reader and a real,
locally-run `fleet.app.app` end to end; every honest-failure path),
`tests/test_storage.py`/`tests/test_fleet.py`/`tests/test_ui_commands.py` (storage
ownership/duplicate rules and the retention cleanup with an injected clock; the endpoint's
auth/ownership/type/size checks; the UI display, escaped).

**Open for P5.3b** (a separate, later package, not built here): the end-to-end encrypted
`diagnostic_bundle` -- see `docs/specification.md` 21.5's own "Decided afterward" paragraph
for why it must stay a one-off, encrypted snapshot and never grow into the cloud's running
storage the way `fetch_logs`'s own retention window does.

## P5.E: local end-to-end test environment (base station VM + scenarios)

Built per the project owner's 2026-09-27 offer ("you can set up a VM
locally with UTM/qemu ... or use Docker to build a complete test
environment"): a Lima VM (`thermoctl-e2e-basestation`, Debian 13 "trixie"
arm64 genericcloud image -- available, no fallback to 12 needed), provisioned
following `image/`'s documented build steps as literally as possible, plus a
Dockerized fleet service (throwaway TLS CA) and a local registry, all under
`tools/e2e/` (not part of CI, run by hand -- see `tools/e2e/README.md`).

**All six required scenarios ran against the real code on this branch and
PASSED**, each with the committed scenario script itself (not just an
ad-hoc rehearsal) re-run once more against a freshly restarted VM right
before this entry was written, to confirm the checked-in scripts -- not
just the commands typed by hand while building them -- actually reproduce
the result:

| Scenario | Result | Evidence (abridged -- full output in the scenario scripts' own runs) |
|---|---|---|
| (a) registration end-to-end | **PASS** | `python -m agent register` (real, unmodified, section 15.3) against the real fleet over real TLS + fingerprint pinning: `201 Created` -> `202`/`200` challenge poll (fleet's real `Retry-After: 60` honoured) -> `200` token. The verification code the agent *displayed* (`8AQ7-QFJZ`) matched the code `protocol.registration.verification_code_for` *independently computed* from the stored public key on the fleet side, byte for byte. `agent_token` and `device_private_key.pem`: mode `0600`, owner `10002:10002`. |
| (b) mounts and permissions | **PASS** | A uid-10002 container, the real image, the real `agent-compose.yml` mounts and `group_add` pattern, wrote `/run/thermoctl-agent/health.env`+`led-status.env` and a sibling of `/var/lib/thermoctl-watchdog/state.env` via the exact temp-file-plus-rename pattern P5.7 depends on -- all landed on the host as `10002:10002`, and the **host-side `thermoctl-watchdog` binary's own `-check-mode`** read them back correctly. Docker socket reachable via `group_add`: raw `GET /_ping` over the mounted socket returned `200 OK` from `dockerd`. |
| (c) watchdog swap | **PASS** | v2 pushed to the local registry, pulled by digest (`localhost:5000/thermoctl-agent`, via `-runtime-repo`, production default untouched -- proved separately by (e)), `desired=v2/proven=v1` written, agent stopped, the already-running `thermoctl-watchdog.service` retagged+recreated the compose-managed container within one 5s poll, the new revision's health report matched `desired` exactly, and the watchdog journal shows **no** rollback line. |
| (d) watchdog rollback | **PASS (both trigger paths)** | d1 (restart-count): a v2 with the same placeholder `python -m agent` CMD as v1 (crashes immediately) hit "three restarts in a row" and rolled back in ~12s (Docker's own backoff, no code change). d2 (the real 10-minute deadline, `AwaitHealthReport`'s `time.Unix(s.Since,0).Add(10*time.Minute)`, unmodified): a v2 that only sleeps (never crashes, never writes a health report) was given a state file with `since` backdated ~9.5 real minutes, so the real, unmodified deadline check only had to wait out the real last ~30s on the real wall clock -- journal: `rolled back to <v1 digest>: no health report for the new digest within the deadline`. No faketime, no `watchdog/watch.go` edit, in either path. |
| (e) no registry pull | **PASS** | `desired` = a well-formed but never-pushed digest -- `docker tag` (purely local) failed repeatedly, exactly as the watchdog journal shows, and the local registry's own container log **line count did not change at all** (`delta=0`) across a 20s window, i.e. zero HTTP requests reached it. No container ever started under that digest. |
| (f) command channel | **PASS** | A `report_now` command created via the storage helper; the real `agent.commands_channel.receive_commands` generator, over the real pinned transport (`agent.transport.build_client`) with the real bearer token from (a), received it over the real SSE stream and posted a real `POST /v1/commands/{id}/result`; the fleet's own `commands` table shows `result_received_at` populated, `successful=1`. |

**What stands in for P5.2 (being built in parallel, not on `main` at the
time of this run):** `agent/loop.py` has no health-report/LED-status/
state-file writer yet -- `python -m agent` with no subcommand only prints
the scaffold message and exits 1. Scenario (a) needs none of that
(registration is real P5.0 code). Scenarios (b)/(c) use a small, explicitly
labelled **test fixture** (a Python snippet doing the exact
temp-file-plus-rename atomic write the real writer will also use, reading
its own "desired" digest back out of the watchdog's own state file so it
can report on itself truthfully) to exercise the **mount/permission
mechanics** (P5.7, the actual subject under test) independent of that
still-missing business logic -- called out inline in both scripts, never
presented as "the real agent loop proved healthy". Scenarios (d)/(e) need
no fixture: a v2 that never writes a health report is exactly what the
current placeholder CMD already does.

**Discrepancies found in `image/`'s own documented build steps** (recorded
for the main session to schedule, not fixed here per the work order) -- the
one substantive one: **`image/common/packages.txt`'s `docker-compose-v2`
does not exist as a Debian 13 "trixie" package** (`E: Unable to locate
package docker-compose-v2`, reproduced against the real trixie repos), and
even Debian's own `docker-compose` package only ships the legacy hyphenated
v1 script, never the `docker compose` (space) v2 CLI subcommand
`watchdog/runtime.go` itself invokes -- stock Debian 13 has no package at
all for that; only Docker's own third-party apt repo does
(`docker-compose-plugin`), which conflicts with `image/README.md`'s own "a
prepared Debian image" premise. Worked around in `tools/e2e/` only (a
manually installed static plugin binary) -- not a proposed production fix.
Four smaller documentation gaps (the hardware `watchdog.service` being
silently host-dependent; no doc stating a host-side uid/gid 10002 is
*not* actually required, only matching numeric ids; no stated
ownership/mode for `/var/lib/thermoctl-agent`; no template for the
watchdog's build-time state file's exact shape) are listed in full, with
the reasoning for each, in `tools/e2e/README.md`'s own "Discrepancies
found" section.

**No bug in `fleet/`, `agent/`, `protocol/`, or `watchdog/` itself was
found** -- every real, non-fixture code path exercised (registration,
transport pinning, the SSE command channel, the watchdog's tag/compose/
rollback/refusal logic) behaved exactly as `docs/specification.md` and this
repository's own docstrings describe it. `image/common/agent-compose.yml`'s
P5.7 directory-mount design (the reason single-file bind mounts were
replaced with whole-directory mounts) was independently re-confirmed here,
against real atomic renames from a real uid-10002 container, not just by
re-reading the comment.

**Environment**: `thermoctl-e2e-basestation`, Debian 13 "trixie" arm64, 2
CPUs / 3 GiB RAM / 8 GiB disk (thin-provisioned; ~2.3 GiB actual host usage
after the full provisioning run plus all six scenarios, comfortably inside
the 15 GB budget). Left **stopped** (not deleted) so it can be reused; see
`tools/e2e/README.md` for how to resume or clean it up.

## Stage-1 command buttons with confirmation in "Eine Wohnung" (P5.1b, section 9)

Turns P3.2's static "commands not yet available" note into the real thing:
one button per `protocol.commands.CommandType` value, each behind a
two-step confirmation (name the apartment and the command, require a
reason, only *then* create it), plus a "Befehle" history section on the
same page. Built entirely on top of P5.1's storage
(`Storage.create_command`/`pending_commands`/`record_command_result`) --
`protocol/`, `agent/`, `watchdog/`, `fleet/auth.py`, and `fleet/ui_auth.py`
are all untouched, and no migration was needed (the `commands` table
P5.1 already created, and the `inventory_audit_log` table P4.1 already
created, both already had everything this package needed).

### Buttons generated from the enum, never a hand-written list (principle 1)

`fleet.ui_apartment.COMMAND_TYPE_LABELS` is a `dict[CommandType, str]`
German-label mapping; `available_commands()` returns
`[(value, label) for command in CommandType]` -- **iterating the enum
directly**, so a value added to or removed from `CommandType` shows up (or
disappears) here automatically, and a mapping that fell out of sync would
fail `tests/test_ui_commands.py::test_command_type_labels_cover_exactly_
the_enum` (`set(COMMAND_TYPE_LABELS) == set(CommandType)`) immediately.
`fleet/templates/ui/apartment.html`'s button list iterates
`detail.available_commands` (itself `available_commands()`, computed in
`build_apartment_detail`) -- there is no second, independently-maintained
list of buttons anywhere in this package.

### Two-step confirmation (`fleet/ui_routes.py`)

`command_confirm_form` (`GET /ui/apartments/{apartment_id}/commands/
{command}/confirm`) names the apartment (id + label) and the command (its
German label) in words and renders a form with a mandatory reason field
(plus a line-count field, 1-500, default 200, only for `fetch_logs`).
`command_confirm_submit` (`POST`, same path) is the **only** caller of
`Storage.create_command_unless_duplicate` in this package -- CSRF-checked
(`check_csrf`), `require_ui_user`-gated, and it re-validates everything a
tampered POST could lie about:

- **An unknown command value in the URL is a 404 that never reaches
  storage** -- `command` is deliberately a plain `str` path parameter, not
  `CommandType`-typed (a FastAPI/Pydantic-typed enum parameter that fails
  to parse is a `422`, not the `404` the work package asks for -- the same
  "never a 422 for a value this module wants to validate itself" reasoning
  `apartment_detail`'s own `days: str | None` already established).
  `_parse_command_type` converts it, in both routes, before either ever
  touches `storage`.
- **A retired apartment gets no buttons on the apartment page
  (`ApartmentDetail.retired`) and the POST refuses one explicitly**,
  checked before `lines`/dedup logic even runs, with its own message
  ("Diese Wohnung ist außer Betrieb, Befehle sind nicht möglich.") -- not
  merely relying on `Storage.create_command`'s own `ValueError` for this
  (that still exists as a second line of defence for any future caller).
- **`lines` is only accepted for `fetch_logs`** -- any submitted `lines`
  field for another command re-renders the confirmation page with HTTP
  `400` and a German error, without creating a command or audit row.
  For `fetch_logs`, `0`/`501`/`abc` all `400`; `1`-`500` are accepted.
- **A mandatory, non-empty `reason`**, length-checked against
  `fleet.ui_inventory.MAX_REASON_LENGTH` (500, the same bound every other
  reason field in this codebase uses) -- both re-rendered as `400` with
  the confirmation form and the original error, mirroring
  `apartment_edit_submit`'s existing pattern exactly.

### Audit logging (`Storage.create_command`, section 20.3: "who, when, why")

`create_command` gained an optional `reason: str | None = None` keyword
parameter and now **unconditionally** writes one `inventory_audit_log` row
in the same transaction as the command row --
`entity_type="command"`, `entity_id` is the wire `command_id` (the `uuid4`
hex string, not the internal sequence number -- the same "no enumerable
primary key over an audit trail either" reasoning `0009_commands.py`
already gives for not using the sequence as the wire id), `action="created"`,
`after_json` carries the command type and `lines`. **`reason` is optional
at the storage layer, not at the UI layer**: P5.1's own pre-existing direct
callers (`tests/test_storage.py`, `tests/test_fleet.py`,
`tests/test_agent_commands_channel.py`, none of them going through the UI)
keep working completely unchanged and simply get an audit row with
`reason=None`; `fleet/ui_routes.py::command_confirm_submit` is the one real
caller that always supplies a non-empty, already-validated one.

### Double-submit protection -- decided, documented, tested

**Decided against a one-time confirmation token, in favour of reusing the
`commands` table itself** (`Storage.create_command_unless_duplicate`,
documented in that method's docstring): a token minted on the
GET confirmation page and checked on the POST would need its own
server-side "already used" state (a session-scoped store, or a new table)
-- this package instead checks within the command-creation transaction
whether an apartment already has a not-yet-resulted command of the exact
same `command_type`/`lines`, created within `fleet.storage
.DOUBLE_SUBMIT_WINDOW` (10 seconds) of `now`; if so, the POST is treated as
already done and redirected exactly like a fresh success, with no second
command and no second audit row. Needs no migration (reuses the existing
`commands` table) and is directly unit-testable with an injected clock.

**The accepted trade-off, stated plainly**: two genuinely separate,
deliberately identical commands (the same button pressed twice on purpose
within the same 10 seconds) are indistinguishable from a double submit,
and the second press is silently dropped. Accepted because
`Storage.pending_commands`'s own established reasoning already makes a
redundant delivery harmless ("re-delivering an already-seen command is
always safe") -- losing one on-purpose, redundant second command in this
narrow window costs nothing operationally, while it closes the actual
target (a reloaded confirmation page, a double-clicked button, a browser
retry after a flaky connection). Tested at both levels: storage
(`tests/test_storage.py`, inside/outside the window, a different command
type, different `lines`, a different apartment, and -- **not** blocked --
once a result has already been stored for the earlier command) and HTTP
(`tests/test_ui_commands.py::test_confirm_post_double_submit_creates_only
_one_command`, two POSTs with the same CSRF-covered form data producing
exactly one stored command, and `::test_confirm_post_after_the_double
_submit_window_creates_a_second_command`, an older command backdated past
the window, allowing a second one).

### P5.1b cross-review fixes

The UI now uses `Storage.create_command_unless_duplicate` for the duplicate
check, command creation and audit entry in one transaction. SQLite takes
a write lock before reading; PostgreSQL locks the apartment row. Concurrent
submissions therefore serialize before checking the ten-second duplicate
window. A duplicate still redirects with HTTP 303 and writes nothing.

A submitted `lines` field on any command other than `fetch_logs` now
re-renders the confirmation form with HTTP 400 and a German error message.
This includes empty and whitespace-only fields; neither a command nor an
audit row is created.

Regression coverage uses real threads and a `threading.Barrier` against a
file-backed SQLite database, asserting exactly one command and one audit
row. Monkeypatched insert failures verify rollback in both directions:
a failed command insert leaves no audit row, and a failed audit insert
leaves no command.

**Cross-review's own verification pass ran in a sandbox that refused to
bind local sockets** (`PermissionError: [Errno 1] Operation not
permitted`), which the agent-side TLS test suite
(`tests/test_agent_commands_channel.py` and others) needs -- their run
showed `31 failed, 7 errors` for exactly that reason and explicitly left a
full passing run "to be verified in an environment that permits those
integration tests." **Done here, fresh venv** (`python3.13 -m venv`,
`pip install -e ".[dev,fleet,agent]"`, no sandbox restriction): `ruff
check .` -- `All checks passed!`; `mypy .` -- `Success: no issues found in
74 source files`; `mypy protocol fleet agent tools` -- `Success: no issues
found in 43 source files`; `python -m pytest -W ignore::ResourceWarning`
**2x**, both exit code 0, **976 passed** each run (`--collect-only` count,
cross-checked against zero failure/error markers in either run -- this
pytest/coverage configuration's own `-q` output ends at the coverage table
with no separate "N passed" summary line, same as every earlier round in
this file), coverage **99%** (3725 statements; run 1: 17 missed, run 2: 16
missed). **The one-line difference is `fleet/storage.py`'s pre-existing
`remove_device` concurrent-race branch** (line 3618, the "lost the race to
a concurrent removal" guard) -- the same timing-based coverage wobble this
file's P5.1 cross-review section already documented for that exact test
class, untouched by this fix.

### "Befehle" history list (section 9)

`Storage.list_commands_for_apartment(apartment_id)` -- newest first,
bounded at `_MAX_COMMAND_HISTORY_ROWS` (200), scoped strictly to
`apartment_id` (mirrors `list_events_for_apartment`/
`list_alarms_for_apartment`'s own "no other apartment's data" guarantee).
`fleet.ui_apartment.build_command_history` turns each row into a
`CommandDisplay` (command label, `created_by`/`created_text`,
`expires_text`, `status_label`, `duration_text`, `error_text`) --
**status is derived purely from already-stored fields, no new column**:
a stored result is authoritative regardless of expiry (`erfolgreich`/
`fehlgeschlagen`, since `record_command_result` is deliberately not gated
on `expires_at`); otherwise `abgelaufen ohne Ergebnis` if `expires_at` has
passed, `zugestellt` if `delivered_at` is set, else `offen`.
`error_text` is truncated to `_MAX_ERROR_TEXT_DISPLAY_LENGTH` (500
characters, with a trailing "…") before it ever reaches the template --
Jinja's own autoescaping (already relied on everywhere else in this
codebase, no `|safe` anywhere) handles the escaping half, verified end to
end by `tests/test_ui_commands.py::test_apartment_page_error_text_is
_escaped` (a real `<script>` tag posted as `error_text`, asserted absent
from the raw response body, `&lt;script&gt;` present instead).

### Templates and CSS

`fleet/templates/ui/command_confirm.html` (new) -- the two-step
confirmation page itself. `fleet/templates/ui/apartment.html`'s "Befehle"
section replaced: a button list (or "außer Betrieb" for a retired
apartment) followed by the history list. `fleet/static/ui/fleet-ui.css`
gained `.command-button`/`.command-history*` rules only -- reuses
`.apartment-tile__tag`'s existing pill styling for the status label rather
than inventing a second one, same "status is text, never colour alone"
rule every other view in this codebase already follows. No inline
style/script anywhere (covered automatically by `tests/test_ui_auth.py
::test_templates_contain_no_inline_style_or_script`'s existing
glob over every template in the directory).

### Tests

`tests/test_ui_commands.py`: the enum-coverage tests
above; GET confirmation (unauthenticated -> 303, shows apartment/command,
`fetch_logs` shows the lines field, unknown command -> 404 never reaching
storage, unknown apartment -> 404, retired apartment shows no form); POST
confirmation (unauthenticated -> 303, missing CSRF -> 422 -- it is a
required `Form` field, mirroring every other CSRF-protected POST in this
codebase -- wrong CSRF -> 403, unknown command/apartment -> 404 never
reaching storage, missing/too-long reason -> 400 re-rendered, retired
apartment refused, `fetch_logs` lines `0`/`501`/`abc` -> 400, `500` ok,
successful creation with every field plus its audit row verified directly
against `CommandRecord`/`InventoryAuditLogRecord`, a tampered `lines` field
on a non-`fetch_logs` command rejected with HTTP 400, the double-submit
pair above);
history-list unit tests for all five status labels (`offen`/`zugestellt`/
`abgelaufen ohne Ergebnis`/`erfolgreich`/`fehlgeschlagen`, the last two via
`record_command_result`, `zugestellt` via `pending_commands` marking
`delivered_at`), duration formatting, error-text truncation; HTTP-level
error-text escaping, another apartment's commands never shown, retired
apartment hides buttons, no inline style/script. `tests/test_storage.py`
gained the audit-row tests (with and without `reason`) and the full
`create_command_unless_duplicate`/`list_commands_for_apartment` matrix
described above. `tests/test_ui_apartment.py`'s pre-existing
`test_every_section_renders_from_real_stored_data` updated for the new
"Befehle" section content (the old "noch nicht verfügbar" note is gone).

**Original P5.1b verification** (fresh venv, `python3.13 -m venv`,
`pip install -e ".[dev,fleet,agent]"`): `ruff check .` and `mypy .`/`mypy
protocol fleet agent tools` both clean; `python -m pytest -W
ignore::ResourceWarning` **2x**: **948 passed** each run, coverage **99%**
(3702 statements, 16 missed) identical across both runs -- `fleet/storage.py` and
`fleet/ui_apartment.py` both at 100%, the handful of remaining misses all
pre-existing and unrelated to this package (`fleet/ui_routes.py`'s own
already-untested branches from earlier packages, `tools/
check_image_config.py`'s own pre-existing gaps).

## P5.2 cross-review fixes: a command id containing a newline broke
at-most-once, no idle retry of the result outbox, on-disk state files not
hardened against a symlink/FIFO

Cross-review of P5.2 (below) found one **required** fix and asked for three
further changes, all four addressed in the same follow-up commit:

1. **Required -- a command id containing a newline broke section 7's
   at-most-once guarantee.** `protocol.commands.Command.id` carries no
   charset restriction at the model level (`Field(min_length=1)` only) --
   a compromised or merely buggy fleet service could send an id like
   `"a\nb"`. `save_agent_state`'s own newline-joined file format
   (`AgentState.executed_ids`, one id per line) would then split such an
   id across two lines on disk; after a restart, `load_agent_state` reads
   it back as two *different*, shorter ids, and the original id is no
   longer recognised as already executed -- reproduced, then fixed by
   validating `command.id`/`rejected.id` against a strict allow-list
   (`agent.loop._COMMAND_ID_PATTERN`, `^[A-Za-z0-9_-]{1,128}$` -- the only
   shape `fleet.storage.Storage.create_command` ever actually produces,
   `uuid4().hex`) **before** anything is executed or persisted, in both
   `execute_command` and `_handle_rejected_command`. An id that fails this
   check is never executed, never spends a dedup slot in
   `state.executed_ids` (not trustworthy enough to remember), and gets
   **no result report at all** (not trustworthy enough to address one to)
   -- logged locally only, via `repr()` (which itself already escapes every
   control character) on top of `_append_local_log`'s own newline
   escaping. Tested: embedded newline, embedded carriage return, embedded
   NUL, over-long (129 characters), whitespace-only ("empty after strip"),
   unicode letters, embedded space -- each rejected, none executed, none
   persisted; a real `uuid4().hex`-shaped id still passes.

2. **Explicit `on_contact` signal from `agent.commands_channel
   .receive_commands`, replacing the log-sniffing `_CloudContactLogHandler`**
   this package's own first version used instead (main-session decision on
   cross-review's suggestion): a small, **additive**, optional callback
   parameter (default a no-op, so every existing call site and test of
   `receive_commands`/`_stream_once` is unaffected) invoked with `True`
   exactly where the SSE stream connects or a `wait=0` poll succeeds, and
   `False` exactly where either fails -- P5.1's own behaviour and test
   suite are otherwise untouched. `agent.loop.run`'s own `_on_contact`
   passes this straight through to `report_led_status`'s `cloud_contact`
   field, no longer inferring channel health from that module's log
   *wording*, which could drift independently of the behaviour it
   described.

3. **Idle flush of the result outbox.** `run`'s `_on_contact(True)` handler
   now also calls the new `agent.commands_channel.flush_outbox` (a thin,
   public wrapper around the existing `_flush_outbox_if_any`) on **every**
   successful poll/stream reconnect, not only when a *new* result happens
   to be reported (`report_result`'s own pre-existing flush-before-post is
   unchanged, still runs too) -- a result that failed to report earlier no
   longer has to wait for the next command to arrive at all. Tested end to
   end: a command's id is pre-seeded into `executed_command_ids` (as if a
   previous run already executed it but its report never made it out) and
   its `CommandResult` pre-seeded into the outbox file directly; its own
   redelivery over the real channel is therefore a no-op duplicate
   (`execute_command` reports nothing for it), so the *only* way its
   buffered result can reach the fleet is the idle flush -- proven by
   monkeypatching `agent.loop._flush_outbox` to perform the real flush and
   then raise a sentinel, stopping the loop deterministically with no
   second command ever created, and confirming the result landed at the
   fleet regardless.

4. **Hardened reads of the agent's own on-disk state files** (executed
   ids, the SSE `Last-Event-ID` bookmark, the result outbox, the local
   log) against a symlink or a non-regular file (FIFO, socket, device,
   directory) planted at one of their paths -- the same defense
   `agent.registration` already established for the private key and
   bearer-token files (`lstat` pre-check, `O_NOFOLLOW | O_NONBLOCK` on
   `open`, an `fstat` re-check on the resulting descriptor to close the
   TOCTOU window), factored into a new, shared `agent/safe_io.py`
   (`read_text_safe`, `append_bytes_safe`) -- deliberately **without**
   `agent.registration`'s additional mode-0600 requirement, since none of
   these four files carries a secret. **Which failure mode applies is a
   per-file decision, not uniform:**
   - `executed_command_ids` fails **closed**: `load_agent_state` lets
     `UnsafeStateFileError`/`OSError` propagate rather than silently
     returning an empty `AgentState` -- doing the latter would quietly
     erase the very at-most-once memory section 7 relies on, letting every
     already-executed command become executable again. `agent.__main__
     ._run_agent` gained a specific `except UnsafeStateFileError` clause
     (a clear, distinct message, not folded into the generic "run failed"
     catch-all) so the CLI still exits cleanly rather than with a raw
     traceback.
   - `commands_last_event_id` and `commands_outbox.json` (both
     `agent.commands_channel`'s own files) fail **safe**: an unsafe path is
     treated exactly like "nothing persisted yet" -- losing the SSE resume
     bookmark only means re-seeing already-delivered commands (harmless,
     P5.2's own id de-duplication already covers it); losing sight of a
     buffered result only delays its delivery, never causes a command to
     execute twice (that guarantee lives entirely in the executed-ids file
     above, a different file).
   - The local log (`agent.loop._append_local_log`) also fails **safe**:
     it is advisory, not a security control, and must not itself crash
     whatever command execution it was in the middle of describing.
   Tested: a symlink and a FIFO at each of the four paths (the FIFO cases
   guarded with a `signal.alarm`, the same tool cross-review used to
   reproduce the class of hang this closes, mirroring
   `tests/test_agent_registration.py`'s own established pattern) -- none
   hang, `executed_command_ids` raises, the other three degrade quietly.
   `agent/safe_io.py` itself has a dedicated test file
   (`tests/test_agent_safe_io.py`, 13 tests: symlink, dangling symlink,
   directory, FIFO, Unix-domain socket, a TOCTOU simulation via
   monkeypatching the `lstat` pre-check, and the fd-not-leaked-on-`fstat`-
   failure branch), at 100% coverage.

**Verification** (fresh `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"`):

```text
$ ruff check .
All checks passed!
$ mypy .
Success: no issues found in 77 source files
$ mypy protocol fleet agent tools
Success: no issues found in 44 source files
```

`python -m pytest -W ignore::ResourceWarning -rA`, run twice: **989 passed**
both times, coverage **99%** both times, identical (3867 statements, 13
missed both runs -- `agent/loop.py`'s four still-deferred stage-2/24 stubs,
`fleet/admin.py`/`tools/check_image_config.py`'s own pre-existing single
lines; `agent/commands_channel.py` and `agent/safe_io.py` both at 100%).
`tests/test_agent_loop_run.py`, `tests/test_agent_commands_channel.py`,
`tests/test_agent_main.py`, `tests/test_watchdog_contract.py`, and
`tests/test_agent_safe_io.py` (82 tests together) run 5 times in a row with
no flakiness. `protocol/`, `fleet/`, `watchdog/` unchanged.

## P5.2 -- stage-1 command execution with local safeguards (section 7,
CLAUDE.md security principle 5)

**Provenance note:** a first draft of this package was produced by a Codex
run that was interrupted before it could verify or commit anything -- its
own sandbox could not open local TLS/socket connections at all (its own
STATUS entry recorded `38 failed, ..., 7 errors`, every one of them a local
`PermissionError` on a socket operation, and said so honestly rather than
hiding it), so none of its own end-to-end tests, nor the full suite, had
actually been run to green. That draft's changes to the already-shipped,
cross-reviewed `agent/commands_channel.py` (P5.1) and to
`tests/test_watchdog_contract.py::test_file_is_not_json` (rewritten to
test something else entirely, to paper over the fact that the draft had
introduced `import json` into `agent/loop.py`, which that very test exists
to catch) were reverted here in full -- P5.2 needs no change to
`agent/commands_channel.py` at all. Two of the draft's genuinely good ideas
were kept, independently re-verified: local-log newline-escaping/truncation
against a hostile, cloud-echoed rejection reason, and representing "fault/
control not yet knowable" (P2.3 still deferred) as a deliberately stale LED
status file rather than a fabricated "no fault" value. Its watchdog-state
fail-closed instinct was also kept, rewritten with distinct, honest error
messages instead of one message conflating two different reasons for
refusal. Everything below is this session's own, fully re-verified design.

`python -m agent run` (new subcommand, `agent/__main__.py`) loads the stored
token and registration file, builds P5.0's pinned client, and loops
`receive_commands` (P5.1, unmodified) -> `execute_command`/
`_handle_rejected_command` (new) -> `report_result` (P5.1, unmodified) --
plus the P5.7 LED bookkeeping below. `agent/loop.py::_HANDLERS` maps every
`protocol.commands.CommandType` value to a handler; a test
(`tests/test_agent_loop_execution.py::test_handler_mapping_covers_exactly_
command_type`) proves the mapping is exactly the enum, so a future stage-1
addition cannot silently fall through unhandled.

**`agent_restart` is genuinely executed; the other four stay honest
failures.** `report_now` ("Herzschlag-Erfassung noch nicht verfügbar
(P2.3)"), `fetch_logs`/`diagnostic_bundle` ("... noch nicht verfügbar
(P5.3: Maskierung und Upload)"), `backup_now` ("... noch nicht verfügbar
(P5.5)") each report a failed result naming the package that will replace
them -- never a fake success. `agent_restart` reports its result first,
**then** asks the main loop to exit via an injectable `exit_fn` (default
`sys.exit`), so the watchdog restarts it via the fixed compose file
(section 17); refused while `_read_watchdog_state` cannot conclusively
establish `desired == proven` -- either an explicit mismatch (a swap is in
flight; a self-stop now is indistinguishable from the watchdog's own step-3
swap signal) or a missing/incomplete state file (**fail-closed**:
`watchdog/state.go`'s own docstring calls an empty/missing `proven` "a sign
of a faulty delivery, not the normal state", so "file absent" is not read
as "safe to restart" here either) -- each with its own distinct error text,
never one message conflating both reasons.

**At-most-once, persisted across restarts.** `AgentState.executed_ids`, the
last `MAX_EXECUTED_IDS` (200) command ids, is now persisted (the open point
the dataclass's own docstring used to flag) as a plain newline-separated
file, one id per line -- deliberately not JSON, the same "every language
can read this with built-in tools" convention `report_watchdog_state`
already established for this module, and checked representatively by the
pre-existing `tests/test_watchdog_contract.py::test_file_is_not_json`. A
201st id evicts the oldest. A duplicate id is **not** executed and
**not** re-reported (`ExecutionOutcome.result is None`, only logged
locally) -- a synthetic second "bereits ausgeführt" report has no real
content of its own to be idempotent about and would risk a `CONFLICT`/`409`
against whatever the first, real result already said; a plain retried
redelivery of an already-*resulted* command is harmless anyway, since
`Storage.pending_commands`'s own `result_received_at IS NULL` filter stops
sending it at all. An **expired** command (`command.expires_at`, compared
against the agent's own clock, `ExecutionContext.now`) is rejected and
**is** reported once (unlike a duplicate: this is its first and only
report) -- clock skew is documented, not solved: nothing here corrects for
a wrong local clock, the same way `fleet/storage.py`'s own alarm/expiry
logic never corrects for the cloud's. A naive `expires_at` (never produced
by the real fleet, not excluded by the model) is assumed UTC. A newer
`protocol_version` (section 18.2) and a malformed/unknown command are both
already turned into a `RejectedCommand` upstream by
`agent.commands_channel._classify`/`_parse_event_data` (P5.1, unmodified)
-- `_handle_rejected_command` reports a failed result when an id is
recoverable, logs locally only when it is not, and applies the same
duplicate-suppression rule.

**Local log** (section 7: "every command and every rejection lands in the
apartment's local log"): a bounded, line-based, append-only file
(`_append_local_log`, 1,000,000 bytes, one `.1` backup), in addition to the
ordinary Python logger. Command ids are logged in plain, readable form
(they are not secrets, and hiding them would defeat this log's own stated
purpose of letting an operator cross-reference what happened without
trusting the cloud) -- but a `RejectedCommand.reason` is untrusted,
cloud-echoed text (a `pydantic.ValidationError` rendering that can contain
bytes lifted straight from a malformed payload, including embedded
newlines): every message is escaped (`\n`/`\r` -> literal `\\n`/`\\r`)
before it is written, so a merely malformed event cannot forge extra,
fake-looking log lines with spoofed timestamps, and one message is capped
to the log's own byte budget so a single absurdly long message cannot
consume it by itself before rotation gets a chance to run.

**`python -m agent run` stops on `CommandStreamAuthError`** (the token is
revoked -- `agent.__main__` turns it into a clear exit-1 message, never an
endless retry loop) and **keeps running on `CommandResultError`** (the
cloud explicitly refused one result report; logged, not fatal -- one
unreportable result must not take down every command after it). A
transient transport failure is already retried inside
`agent.commands_channel.receive_commands`'s own fallback loop and never
surfaces here at all.

**LED status file `cloud_contact`** (P5.7's `report_led_status`, this
package's first real caller): flips to `lost` via a small
`logging.Handler` that watches `agent.commands_channel`'s own logger for
the WARNING it already emits on a stream/poll failure -- **no change to
that module** (an earlier draft added a channel-level `on_contact`
callback parameter for this; reverted, since the existing log output was
already enough). Flips back to `ok` the moment another item is actually
received. Written `lost` immediately before `CommandStreamAuthError`
propagates.

**`fault`/`control` stay honestly unknown until P2.3, not a fabricated
"none"/"ok"** -- `report_led_status`'s `fault`/`control` parameters are now
`Optional` (`None` written as the literal `unknown`); when either is
`None`, `timestamp` is deliberately written as `0` instead of the real
time, so `cmd/thermoctl-leds`'s own already-existing staleness rule
(`agentStatusFresh`, docs/STATUS.md's own P5.7 entry: an aged-out report is
"treated as unknown, never as 'still good'") makes both LEDs fall back to
their documented conservative pattern -- **without any change to
`watchdog/` at all**, reusing the file format's existing "we do not
actually know" mechanism rather than inventing a new one.
**Trade-off, documented, not silently accepted:** because `cloud_contact`
shares that same file and timestamp, forcing it stale to keep `fault`/
`control` honest also means LED 1's own `cloud_contact`-dependent
distinction ("two short blinks" vs. "steady on") is not yet *visible*
through the LEDs either, even though `cloud_contact` itself is still
written accurately on every call (inspectable directly, or via
`-check-mode`) -- both LEDs staying on conservative slow blink until P2.3
lands is the intended, safe degradation this file format was built with,
not a bug.

**Tests:** `tests/test_agent_loop_execution.py` (unit-level, 30 tests) --
the handler mapping's exact coverage; each not-yet-available command's
honest failure text; `agent_restart` success, refusal on mismatch, and
refusal on a missing/incomplete state file (fail-closed); duplicate-id
suppression, including across a simulated restart via
`load_agent_state`/`save_agent_state`; the 200-id cap evicting the
oldest; expiry (including the exact-boundary "not yet expired" case, and
a naive `expires_at` treated as UTC); `RejectedCommand` handling (no
id/logged only, id present/reported, redelivered/not re-reported); the
local log's newline-escaping, per-message truncation, and rotation;
`report_led_status`'s stale-vs-fresh timestamp behaviour for unknown vs.
known fault/control.

`tests/test_agent_loop_run.py` (end-to-end, 7 tests, real `fleet.app.app`
over real TLS, no mock of TLS or of the real app's behaviour anywhere in
the file): a command created via `Storage.create_command` is executed by
`run` and its result ends up stored at the fleet (queried directly from
`CommandRecord`); the result is reported before `exit_fn` runs (checked by
having `exit_fn` itself look at the fleet's own storage); a refused
`agent_restart` (swap pending, `_read_watchdog_state` monkeypatched to
return two different answers in sequence, decoupling this from any real
timing between two commands delivered in the same SSE session) does not
stop the loop, a second, accepted one does; `CommandStreamAuthError` stops
the loop and marks `cloud_contact=lost`; a real stream drop (nothing
listening at all) marks `cloud_contact=lost`, observed from inside the
injected `sleep` callable, then `ok` again once the server comes back and
delivers a command; a `RejectedCommand` (a command's stored
`protocol_version` bumped directly in the database to simulate section
18.2) is reported and does not stop the loop; a result report refused by
the fleet (`CommandResultError`, via a monkeypatched `receive_commands`
that redelivers an already-resulted command -- the real channel's own
`result_received_at IS NULL` filter means the real fleet would otherwise
never redeliver it at all, so this one scenario needs the seam) is logged
and does not stop the loop either.

`tests/test_agent_main.py` gained coverage for the new `run` subcommand:
requires prior registration; builds the pinned client from the stored
registration file and token (monkeypatched `loop.run` to assert on what it
was called with, never a real network call); turns `CommandStreamAuthError`
into exit code 1 with a clear message; never echoes the token or any
other secret into an error message, including for an unparsable
registration file.

**Verification** (fresh `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"`):

```text
$ ruff check .
All checks passed!
$ mypy .
Success: no issues found in 75 source files
$ mypy protocol fleet agent tools
Success: no issues found in 43 source files
```

`python -m pytest -W ignore::ResourceWarning` (`-rA`, since this version of
pytest -- 9.1.1 -- otherwise prints no final summary line under this
repo's own `-q` default in `addopts`), run twice: **944 passed** both
times, coverage **99%** both times (3795 statements; **13** missed on the
first run, **14** on the second -- the one-line difference is
`fleet/storage.py:2527`, a **pre-existing**, already-documented
timing-based flake in that file's own `threading.Thread` concurrency tests
(see this file's own P5.1 cross-review entry above for the identical
wobble reported there), not touched by this package; `agent/loop.py` itself
stayed at the same 4 missed lines -- `collect_heartbeat`/`send_heartbeat`'s
`NotImplementedError`s and two further stage-2/24 stubs -- in both runs).
`tests/test_agent_loop_run.py`, `tests/test_agent_commands_channel.py`,
`tests/test_agent_main.py`, and `tests/test_watchdog_contract.py` (58
tests together) run 5 times in a row with no flakiness at all (same 58
passed each time). `protocol/`, `fleet/`, `watchdog/` unchanged
(`git diff --stat HEAD -- protocol fleet watchdog` is empty).

**Still missing, explicitly out of scope for this package, tracked here
instead of invented:** `report_now`'s real heartbeat collection (P2.3);
`fetch_logs`/`diagnostic_bundle`'s masking and upload (P5.3); `backup_now`'s
encryption (P5.5); `reconcile_desired_state` (P5.4) -- once it exists,
`_handle_agent_restart`'s watchdog-state read and any future
desired-state write must be considered together (both touch the same
state file) -- noted here, not solved.

## P5.1 cross-review fixes: silently-retried auth failures, a global-sequence
`Last-Event-ID` that could hide an apartment's own commands

Cross-review of P5.1 (below) reproduced two defects, both fixed in the same
follow-up commit:

1. **A revoked/invalid token on the command channel was retried forever
   instead of surfaced.** `agent.commands_channel._stream_once`/`_poll_once`
   raised the generic `CommandStreamError` for *any* non-200 response,
   including 401/403 -- and `receive_commands`'s own fallback loop catches
   `CommandStreamError` exactly like a dropped connection, so a revoked
   token looked indistinguishable from a network hiccup and was polled
   forever, never surfaced to the caller. Inconsistent with
   `agent.heartbeat_sender.HeartbeatAuthError` (the equivalent case for the
   heartbeat channel) and with this same module's own `CommandResultError`
   (which already propagated any non-204, 401/403 included). **Fixed** with
   a new, deliberately *separate* exception, `CommandStreamAuthError` (not
   a subclass of `CommandStreamError` -- an `except CommandStreamError`
   clause must never accidentally also catch an auth failure by
   inheritance): `_raise_for_non_200`, a small shared helper both
   `_stream_once` and `_poll_once` now call, raises it for 401/403 and the
   ordinary `CommandStreamError` for anything else non-200.
   `CommandStreamAuthError` is not listed in either of `receive_commands`'s
   two `except` tuples, so it always propagates straight to the caller.
   **Tested against the real fleet app** with a token actually revoked via
   `Storage.remove_device(expected_assignment_id=...)` (mirroring
   `tests/test_agent_heartbeat_sender.py`'s own identical pattern for the
   heartbeat side) -- `_stream_once`, `_poll_once`, and the full
   `receive_commands` end-to-end all raise `CommandStreamAuthError`
   directly (`tests/test_agent_commands_channel.py`). The generic
   `CommandStreamError` path (a non-200, non-401/403 response) is now
   covered separately via a fixed-status `httpx.MockTransport` -- the real
   fleet app has no code path that returns anything else for this
   endpoint, so this is purely the branch logic, the same established
   `MockTransport` exception `tests/test_agent_heartbeat_sender.py`'s own
   module docstring already documents.

2. **A global command sequence, combined with an untrusted client-supplied
   `Last-Event-ID`, could hide an apartment's own pending commands** --
   fixed in **two rounds**, the first of which was itself incomplete.
   `CommandRecord.id` (the SSE stream's own monotonic sequence number,
   shared by *every* apartment's commands, see that column's own
   docstring) means a `Last-Event-ID` legitimately issued for one apartment
   is, structurally, also a syntactically valid resume point for any other
   apartment -- `Storage.pending_commands` trusted `after_sequence` as-is.

   **Round 1 reproduction:** ten commands for apartment B (sequences
   1-10), then one for apartment A (sequence 11); `pending_commands
   ("apartment-a", 16, now)` returned `[]` -- 16 was never sent to A.
   **Round 1 fix (incomplete):** clamp `after_sequence` to `0` whenever it
   exceeds this apartment's own *maximum* command sequence (`MAX(id)
   WHERE apartment_id = ...`).

   **Round 2 reproduction (re-review found the same class of gap
   survived):** A at sequences 1 and 11, B at 2-10;
   `pending_commands("apartment-a", 2, now)` returned only `[11]` -- A's
   own still-pending sequence-1 command was skipped, because `2` is *less
   than* A's own maximum (11), so the round-1 `MAX()`-only clamp let it
   through even though A was never actually sent sequence 2 (it belongs to
   B, sitting strictly *between* A's own two sequences). A bound
   ("not beyond the maximum") can never catch a gap value like this --
   only a bound *and* the exact set of values.

   **Round 2 fix -- membership, not a bound:** `after_sequence` is now
   honoured only if a `CommandRecord` with exactly that `id` *and*
   `apartment_id` actually exists (`EXISTS ... WHERE apartment_id = :a AND
   id = :after`, replacing the `MAX()` comparison entirely) -- checked
   against **any** of this apartment's own commands, not only its
   still-pending ones, since a legitimate resume point just as often names
   an already-delivered, already-resulted, or already-expired command as a
   pending one. Anything that fails this check -- beyond the apartment's
   range, in a gap between two of its own sequences, or simply another
   apartment's value -- falls back to `0`.

   **Also guarded before ever being bound as a SQL parameter:** a
   non-positive `after_sequence`, or one larger than `_MAX_COMMAND_SEQUENCE`
   (`2**63 - 1`, the largest value a `BIGINT` id column can hold), is
   rejected outright -- nothing stops a client from sending, say, `2**128`
   in a `Last-Event-ID` header, and binding that directly risks an
   `OverflowError`/driver-level failure instead of the ordinary "not a
   valid resume point" fallback every other out-of-range value gets.

   **A per-apartment sequence counter was considered and rejected in both
   rounds** (main session decision, see `Storage.pending_commands`'s own
   docstring for the full reasoning): it would need its own migration and
   its own concurrency-safe allocation scheme for no operational gain,
   since re-delivering an already-seen command is always safe -- P5.2's
   own id de-duplication (`AgentState.executed_ids`) and
   `Storage.record_command_result`'s `DUPLICATE_IDENTICAL` outcome already
   make a redundant delivery harmless.

   **Tested** (both rounds' reproductions kept, not replaced): storage
   level (`tests/test_storage.py`) -- beyond-range (round 1), between-range
   (round 2), legitimate resume still skipping the apartment's own older
   commands, non-positive values (`-1`, `0`), and the `BIGINT`-overflow
   guard (`2**63 - 1` -- still a plausible value, correctly rejected by the
   membership check; `2**63` and `2**128` -- rejected before binding at
   all, no exception raised). HTTP level (`tests/test_fleet.py`): the same
   beyond-range and between-range reproductions via `GET
   /v1/commands?wait=0` with a real `Last-Event-ID` header.

**Coverage wobble note (round 1):** the reviewer observed coverage wobble
in pre-existing `fleet/storage.py` lines (~2447, ~2500-2502, inside
`confirm_device`'s "replace previous device" branch) in one of three runs.
That code path is untouched by this package or by either round of this
fix -- neither P5.1 nor either follow-up adds, removes, or reorders any
test that exercises `confirm_device`. Every fresh-venv run done for this
package (both rounds) showed `fleet/storage.py` at a stable 100% with an
identical `TOTAL`; the wobble, if real, is pre-existing and unrelated to
this package's own changes.

**Verification** (fresh venv, round 2): `ruff check .`, `mypy .`, `mypy
protocol fleet agent tools` all clean; `python -m pytest -W
ignore::ResourceWarning` **3x**: **902 passed** each run, coverage **99%**
(3596 statements, 16 missed) identical across all three. (A stray extra,
ad-hoc run outside this official 3x once showed 19 missed instead of 16 --
a pre-existing, timing-based flake in this file's own `threading.Thread`
concurrency tests, e.g. `test_remove_device_concurrent_double_removal_only
_one_wins`/`test_partial_unique_index_for_assignments_is_safe_under
_concurrent_calls`, none of them touched by this package; not reproduced
in any of the 3 official runs above.)

## SSE command channel between fleet and agent (P5.1, sections 3, 7, 18.2)

**`PROTOCOL_VERSION` bumped to 3** for `protocol.commands.Command.protocol_version`
(new, required field) -- section 18.2's "a number that increases with every
change to the models" read literally, exactly as the project owner's
2026-09-26 decision (recorded for version 2 above) already established.
`CommandType` itself is untouched (principle 1, the command list stays
closed).

### Protocol: `Command.protocol_version`

A `Command` now carries the `PROTOCOL_VERSION` it was created under --
section 18.2's "the agent rejects commands of a newer version it does not
know ... reports that as a result, and keeps running" needs a value to
compare against; without this field there was nothing to reject *on*.
Required (`Field(ge=1)`), not defaulted at the model level: `fleet.storage
.Storage.create_command` is what actually stamps it, from
`protocol.version.PROTOCOL_VERSION` at creation time.

### Fleet: storage, `GET /v1/commands`, `POST /v1/commands/{id}/result`

**Migration `0009_commands.py`** (`down_revision="0008"`, `0001`-`0008`
untouched): a `commands` table. `id` (autoincrement) doubles as the SSE
stream's own monotonically increasing sequence number -- a database
autoincrement primary key already guarantees "only ever goes up", so a
separate `sequence` column would only duplicate it (the same reasoning
`HeartbeatRecord.id`/`EventRecord.id` already rely on). `command_id` (the
wire `id`, a `uuid4` hex string) is a **separate**, unique, indexed column
-- deliberately not the primary key, so an agent (or an attacker holding a
valid token) cannot enumerate other apartments' command counts by
incrementing a path segment (mirrors `0008`'s own `external_id` reasoning).
`lines` (`fetch_logs` only), `created_at`/`expires_at` (`expires_at` always
`created_at` + 15 minutes, computed once at creation, never derived later),
`created_by` (UI username, for P5.1b), `protocol_version`, `delivered_at`
(set once, first delivery, via either SSE or a `wait=0` poll), and the
result fields (`successful`/`duration_s`/`error_text`/`result_received_at`).
`alembic.autogenerate.compare_metadata` stays empty against `0001`-`0009`.

**`Storage.create_command(apartment_id, command_type, *, lines, ui_username,
now)`** -- the function P5.1b's UI buttons will call, built and tested here.
Refuses (`ValueError`) an unknown apartment, a retired apartment
(`ApartmentRecord.state == "retired"`), or `lines` set for anything other
than `CommandType.FETCH_LOGS`. Returns the wire `Command`
(`id=uuid4().hex`, `expires_at = now + 15 min`, `protocol_version =
PROTOCOL_VERSION` at call time).

**`Storage.pending_commands(apartment_id, after_sequence, now)`** -- not
expired (`expires_at > now`), no result yet (`result_received_at IS NULL`),
sequence greater than `after_sequence`, ordered by sequence. **Expired
commands are never returned**, filtered here, not by a status column --
section 7: "if an apartment comes back after three days, an old command is
not executed any more". Marks `delivered_at` for every row it returns that
does not have one yet -- the one place both SSE delivery and `wait=0`
polling go through, so "first delivery" bookkeeping is not duplicated in
both callers.

**`Storage.record_command_result(command_id, apartment_id, result, now)`**
-- returns a `RecordCommandResultOutcome`: `NOT_FOUND` (unknown command id,
*or* one belonging to a different apartment -- deliberately
indistinguishable, mirroring `fleet/auth.py`'s "wrong token vs. unknown
apartment" precedent), `STORED` (first report), `DUPLICATE_IDENTICAL` (a
second report with the exact same `successful`/`duration_s`/`error_text` --
an agent retry after a lost response, not an error), or `CONFLICT` (a
second report that *disagrees* with what is stored -- the first,
authoritative report is never overwritten). **Decided: idempotent-same-
content is `204`, not `409`** -- a plain retry must not become a permanent
error for a well-behaved agent that simply never saw its own `204`; only a
genuinely *conflicting* second report is `409`. Not gated on `expires_at` --
expiry only ever gates delivery, never whether an already-delivered
command's result may still be reported.

**`GET /v1/commands`** (`commands_stream`), behind the existing
`require_apartment_token_by_hash` (P1.1): `?wait=0` is the section-3
fallback, one-shot -- **decided and documented here: a plain JSON list of
`Command` objects**, not a one-event SSE stream (simpler for a polling
client, no SSE parser needed for the fallback path at all); the response
also carries `Retry-After: 60` (section 3's own cadence, the same
convention `request_token_challenge` already uses). The open-connection
case is `sse_starlette.EventSourceResponse` wrapping
`_stream_command_events` (a module-level, directly testable async
generator, deliberately pulled out of the route closure -- `TestClient`
does not read an `EventSourceResponse` incrementally, so
`tests/test_fleet.py` drives this generator directly with a fake
`is_disconnected` callable rather than through a real streaming HTTP round
trip): one SSE event per pending command, `id: <sequence>` (what
`Last-Event-ID` resumes from), `data: <Command JSON>`, a `retry:` hint;
polls `Storage.pending_commands` at a small, configurable interval
(`FLEET_COMMANDS_POLL_INTERVAL_S`, default 1 s) rather than busy-looping;
ends on client disconnect (`request.is_disconnected()`, checked before
every poll); keep-alive comments (`: ping`) are `EventSourceResponse`'s own
built-in mechanism (`ping=`, `FLEET_COMMANDS_SSE_PING_INTERVAL_S`, default
15 s), not reimplemented. `Last-Event-ID` parsed via `_last_event_id`:
absent or unparsable both fall back to `0` ("everything still pending"),
never a 500 -- CLAUDE.md principle 5 applied to a client-supplied value.

**`POST /v1/commands/{id}/result`**: path `id` must equal `result.id` --
checked before storage is ever touched, `400` on a mismatch. Otherwise maps
`Storage.record_command_result`'s outcome via `_COMMAND_RESULT_STATUS`:
`NOT_FOUND` -> `404`, `STORED`/`DUPLICATE_IDENTICAL` -> `204`, `CONFLICT`
-> `409`.

### P5.0 transport fix this package needed: no more eager response buffering

**Found while building the agent side**: `agent.transport._PinnedTransport
.handle_request` used to read the *entire* response body eagerly
(`b"".join(response.stream)`) before ever returning to `httpx` -- correct
for the short request/response calls P5.0 built (registration, heartbeat),
but P5.1's SSE stream is the first caller that reads a response
incrementally over a connection meant to stay open; the eager join blocked
forever on a body that is never meant to end. Fixed with `_LazyHttpcoreStream`,
a small `httpx.SyncByteStream` wrapping the `httpcore` stream **without**
reading it -- correct for both call styles `httpx.Client` produces: an
ordinary `client.get`/`.post` (`stream=False`) still has `httpx.Client.send`
call `response.read()` immediately, consuming and closing exactly as
before; `client.stream(...)` (what `httpx_sse.connect_sse` uses) instead
leaves iteration and closing to the caller's own `with` block, which is
what makes a long-lived, incrementally-delivered response possible at all.

**A second, related gap found and fixed in the same pass**: iterating that
lazy stream after a connection drops mid-read raised a raw
`httpcore.RemoteProtocolError` (or `ReadTimeout`/`ConnectError`/...),
never translated into the matching `httpx.*` exception every caller in
this codebase already catches (`except httpx.TransportError`) -- because
the old code's only exception handling (`httpcore.ConnectError` ->
`httpx.ConnectError`) covered just the *initial* `handle_request` call, not
the stream read that follows. `_map_httpcore_exceptions` (mirrors
`httpx`'s own internal `HTTPTransport`/`map_httpcore_exceptions`, applied
around both the initial request and the stream iteration) closes this for
every exception `httpcore` can raise, ordered most-specific-first
(`_HTTPCORE_EXCEPTION_MAP`). Both fixes are exercised for real:
`tests/test_agent_commands_channel.py`'s stop/restart-the-server test would
not pass without either -- reproduced by running that test directly before
the fix and observing it hang, then hit an uncaught `httpcore
.RemoteProtocolError`, before both were fixed. `tests/test_agent_transport
.py` gained direct tests for `_map_httpcore_exceptions` (an unrelated
exception passes through unchanged; `CertificateFingerprintMismatch`
passes through unchanged) -- `agent/transport.py` at 100% coverage.

### Agent: `agent/commands_channel.py` (new)

**`receive_commands(client, last_event_id_path, *, fallback_poll_interval_s=60.0,
sleep=time.sleep)`** -- a `Generator[Command | RejectedCommand, None, None]`
(typed `Generator`, not the narrower `Iterator`, specifically so a caller
can `.close()` it, which every short-lived test in
`tests/test_agent_commands_channel.py` does). Over P5.0's **pinned** client
only, never a plain `httpx.Client`. Outer loop: attempt the SSE stream
(`_stream_once`, `httpx_sse.connect_sse`, sending `Last-Event-ID` from
whatever was last persisted); on failure or a non-200 response
(`CommandStreamError`), do exactly one `wait=0` poll (`_poll_once`), yield
what it returned, sleep for the fleet's own `Retry-After` (clamped via the
same `agent.registration._parse_and_clamp_retry_after` P5.0 already built,
reused here rather than duplicated), then try the stream again -- this
*is* "polling every 60 s" (section 3), produced by the outer loop's own
cadence, not a separate polling code path.

**Classification, never executed here** (P5.2's job): a structurally
malformed event or an unknown `CommandType` (both collapse into the same
`pydantic.ValidationError` -- the command list is closed at the model
level, principle 1) becomes a `RejectedCommand(id, reason)`, `id` recovered
best-effort from the raw JSON if legible, `None` otherwise. A `Command`
that parses fine but whose own `protocol_version` is newer than this
agent's `PROTOCOL_VERSION` is also turned into a `RejectedCommand` (section
18.2) -- `_classify` is shared by both the SSE and the `wait=0` path.

**`Last-Event-ID` persisted eagerly, before yielding** (temp file + atomic
`Path.replace`, same pattern as `agent.heartbeat_sender`'s buffer and
`agent.loop.report_watchdog_state`) -- **decided**: this is a
transport-level bookmark ("which events has this stream already
delivered"), not an execution-safety mechanism; section 7's own "the agent
remembers the last 200 ids" (P5.2's future `AgentState.executed_ids`) is
what actually guards against ever *executing* a command twice. Persisting
eagerly means a crash between receiving an event and finishing whatever is
done with it re-receives that one event on reconnect -- it never silently
drops a command by advancing the bookmark past one nothing ever actually
saw. **The `wait=0` fallback never advances this bookmark at all** (there
is nothing per-item to advance it *with* -- the fallback response is a
plain `Command` list, no sequence numbers) -- harmless by the same
reasoning: a still-pending command simply keeps reappearing on every poll
until it is executed and reported, never silently lost.

**`report_result(client, result, *, outbox_path)`** -- mirrors
`agent.heartbeat_sender.send_heartbeat`'s own buffering shape: flushes any
already-buffered results first (`_flush_outbox_if_any`), then posts
`result`; a transport failure buffers it (capped at `MAX_OUTBOX_RESULTS`,
200, oldest dropped first); an explicit refusal (`404`/`400`/`409`, a
non-2xx that is *not* a transport failure) raises `CommandResultError`
immediately, **not buffered** -- mirrors `HeartbeatAuthError`'s own "do not
retry a rejection with the same content forever" reasoning. A buffered
result the cloud *still* refuses on retry is dropped, logged, not kept
forever either.

### Tests

`tests/test_fleet.py`: create -> pending -> delivered via SSE with the
correct sequence `id:`/`data:` (driving `_stream_command_events` directly,
see above for why not through `TestClient`'s own streaming transport);
`Last-Event-ID` resume (both the SSE generator and the `wait=0` fallback,
including a malformed header falling back to `0`); expired never delivered
(injected clock); another apartment's commands never appear (SSE and
`wait=0`); `wait=0` with nothing pending; unauthenticated -> `401`; result
endpoint: wrong apartment -> `404`, path/body id mismatch -> `400`, double
identical report -> `204`, double conflicting report -> `409`, stored
fields verified directly against `CommandRecord`. `tests/test_storage.py`:
`create_command` refuses unknown/retired apartments and `lines` on
non-`fetch_logs`, stamps expiry/version/a fresh id; `pending_commands`
scoping, expiry, delivered-marking, result-exclusion; `record_command_result`
outcomes; migration `0009` up/down and `compare_metadata`.

`tests/test_agent_commands_channel.py` (21 tests, real `fleet.app.app`,
real TLS, `tests/tls_support.py`): holds an SSE connection and receives a
command; `Last-Event-ID` resume across a fresh generator (a simulated agent
restart); never sees another apartment's command; **the full stop/restart
scenario** -- a real `uvicorn` server is stopped mid-stream
(`timeout_graceful_shutdown=1`, needed so `.stop()` actually closes the
in-flight connection instead of waiting forever for it to end on its own),
the interrupted read is caught, the `wait=0` fallback is attempted (and
also fails, server still down), and -- driven entirely by the injected
`sleep=` callable, never a real wait -- the server comes back up inside
that same callable and the stream resumes, delivering a command created
while the server was down; a companion test (`_stream_once` monkeypatched
to always fail) exercises the opposite split, stream down but the `wait=0`
poll succeeding against the real, still-up server, including the real
`Retry-After: 60` clamp. Malformed event, unknown command type, and a
newer `protocol_version` are all surfaced as `RejectedCommand`, never a
`Command`. `report_result`: success; failure -> outbox -> retried on next
call; a pin mismatch delivers nothing (`tests.tls_support
.run_recording_tls_server`, the `received` list stays empty); the outbox's
own cap, transport-failure-during-flush, and drop-on-repeated-refusal
branches. `agent/commands_channel.py` at 100% coverage.

Full suite (fresh venv): **890 tests**, coverage **99%** (3585 statements,
16 missed -- `agent/loop.py`'s still-deferred stage-2/24 command stubs and
`fleet/admin.py`/`tools/check_image_config.py`'s own pre-existing single
gaps, none of them touched by this package) -- identical across 3
consecutive runs; `ruff check .` and `mypy .` / `mypy protocol fleet agent
tools` both clean; `tests/test_agent_commands_channel.py` and
`tests/test_fleet.py` run 5 times in a row with no flakiness. `watchdog/`:
`go vet ./...` clean, `go test ./...` green (67 tests, unchanged by this
package), `watchdog/check_contract.sh` passes (`protocol/` changed --
`Command.protocol_version`; the watchdog's own state/health/LED file
contract is untouched by that field).

**Still missing, explicitly out of scope for this package, tracked here
instead of invented:** P5.1b (UI buttons that call `Storage.create_command`)
-- this package only builds and tests the storage function; command
execution, id de-duplication, and expiry *checking on the agent side*
(P5.2) -- `receive_commands` only ever yields items, nothing here executes
anything; masking of `fetch_logs`/diagnostic-bundle content (P5.3).

## P5.0 second cross-review fix: FIFO/device/socket refused before `open`,
not just symlink and mode (main session read-back) **SR**

Re-review of the fix below found one more reproduced gap: `_assert_safe
_private_file` checked for a symlink and for mode 0600, but not for the
file *type* -- a **FIFO** created at the private-key or token path with
mode 0600 passes both of those checks, and the subsequent
`os.open(path, O_RDONLY | O_NOFOLLOW)` then **blocks forever** waiting for
a writer to open the other end of the pipe (the reviewer reproduced this
directly, with a 5-second `signal.alarm` guard).

**Fixed with two layers**, not one:

1. `_assert_safe_private_file` now also checks `stat.S_ISREG(...)` via the
   same `lstat` call already used for the symlink/mode checks -- a FIFO, a
   socket, a character/block device, or a directory at this path is
   refused right here, before any `open` call is attempted at all.
2. **Extra safety against the TOCTOU race between that `lstat` and the
   `open` a few lines later** (the path could in principle be replaced in
   between): `_read_private_file` now opens with `O_NONBLOCK` in addition
   to `O_NOFOLLOW` (a no-op for a regular file -- POSIX defines it as
   meaningful only for FIFOs and some device files, so opening a FIFO that
   slipped in during the race returns immediately instead of blocking),
   and re-checks `stat.S_ISREG` a second time via `fstat` on the now-open
   file descriptor itself (not the path again, which would just reopen the
   same race) before a single byte is read.

**Tests** (`tests/test_agent_registration.py`): a 0600 FIFO, guarded by the
same kind of `signal.alarm`-based timeout the reviewer used
(`_AlarmGuard`, 5 s) so a regression here fails fast and loud instead of
hanging the test suite; a directory at the key path; a Unix domain socket
(bound under a short-lived `/tmp` directory directly, since `AF_UNIX` path
lengths are far shorter than pytest's own nested `tmp_path`); and one test
that monkeypatches `_assert_safe_private_file` itself into a no-op to
exercise the second, `fstat`-on-the-open-fd defense in `_read_private_file`
directly and independently of the `lstat` pre-check (the TOCTOU race the
two layers together close is not practical to reproduce with real timing).
`agent/registration.py` back to 100% covered.

**Verification after this fix** (fresh venv): `ruff check .`, `mypy .`,
`mypy protocol fleet agent tools` all clean; full suite passed with the
same `TOTAL` coverage as the previous round (see below for the exact
count from this run).

## P5.0 cross-review fixes: Retry-After clamping, CLI exception handling,
file-tampering checks (main session read-back) **SR**

Cross-review of P5.0 (below) found five gaps, all fixed in the same
follow-up commit:

1. **`agent/registration.py::_poll_for_challenge` trusted the server's
   `Retry-After` header unclamped.** A `-5` reached `time.sleep(-5)`
   directly (`ValueError: sleep length must be non-negative`, crashing the
   poll loop); `99999999` parked the agent for ~3 years. CLAUDE.md security
   principle 5 ("the agent is the security boundary") applies to a *number*
   the cloud supplies, not only to a command -- a pinned, verified
   connection proves *who* the agent is talking to, not that the values it
   sends are sane. Fixed with `_parse_and_clamp_retry_after`: absent,
   empty, unparsable (`"abc"`), or non-finite (`float("nan")`/`float("inf")`
   both parse *without raising*, so the bare `except ValueError` alone
   would not have caught them) all fall back to the caller's own
   `poll_interval_s` default, and every value -- parsed or fallen back to --
   is then clamped into `[1.0, 300.0]` seconds before it ever reaches
   `sleep`. Tested directly (`-5`, `0`, `1e9`, `"nan"`, `"inf"`, `"-inf"`,
   `"abc"`, absent, a normal `"30"`) and once more through the real poll
   loop via `httpx.MockTransport` (`test_poll_for_challenge_clamps_a
   _malicious_retry_after_end_to_end`) to prove the clamped value actually
   reaches the injected `sleep` callable, not just the helper function in
   isolation.
2. **`agent/__main__.py` had 0% coverage, and its one `except` tuple did
   not include `httpx.TransportError` or `pydantic.ValidationError`.** A
   certificate pin mismatch (`agent.transport
   .CertificateFingerprintMismatch`, itself an `httpx.TransportError`
   subclass) or an unreachable server therefore crashed the CLI with a raw
   traceback instead of the same clean "registration failed: ..." message
   at exit code 1 every other failure already got. Both exception types
   added to `_run_register`'s `except` tuple, each with a comment
   explaining exactly which real failure it closes. `tests
   /test_agent_main.py` (new, 9 tests, 100% coverage of `agent/__main__.py`)
   covers: register success, already-registered, no subcommand, an unknown
   subcommand (argparse's own exit code 2), `--help` (exit 0), a pin
   mismatch, an unreachable server, a malformed server response
   (`pydantic.ValidationError`), and `RegistrationError` -- all via
   monkeypatching `agent.__main__.register` itself with a controllable
   stand-in (a plain unit-testing seam for this module's own CLI plumbing,
   not a mock of TLS or of registration's own logic, both already covered
   for real in `tests/test_agent_registration.py`).
3. **The private key and token files were writable/readable without
   checking for a symlink or an over-wide mode.** `_write_private_file` now
   opens with `O_CREAT | O_EXCL | O_NOFOLLOW` (both callers -- a freshly
   generated key, a freshly issued token -- only ever write a path already
   established not to exist, so `O_EXCL` turns a stale leftover or a race
   into a loud `FileExistsError` instead of silently overwriting it, and
   `O_NOFOLLOW` refuses to write through a symlink planted at that path).
   Reading (`load_or_create_private_key`, `load_token`) now goes through
   `_read_private_file`, which checks via `lstat` first (`_assert_safe
   _private_file`: refuses a symlink, and refuses a mode other than exactly
   0600, both with a clear `InsecureKeyFileError` -- **no silent chmod, no
   silent unlink-and-follow**) and additionally opens with `O_NOFOLLOW`
   itself, closing the TOCTOU gap between the `lstat` check and the actual
   read. `InsecureKeyFileError` subclasses `RegistrationError`, so
   `agent.__main__`'s existing catch-all already covers it too. Nine new
   tests cover: a symlink (and a *dangling* symlink, which `Path.exists()`
   alone would miss -- caught via `path.exists() or path.is_symlink()`) for
   both the private-key and token files, a mode wider than 0600 for both,
   the wrong key type on disk (an RSA key where an Ed25519 one is
   expected), `_write_private_file` refusing to overwrite an existing file
   and refusing to write through a symlink, and the `O_NOFOLLOW` read path
   directly.
4. **A leak-proof test for the agent's own two secrets, mirroring the
   fleet side's `tests.test_device_registration_v1
   ::test_raw_token_never_stored_or_logged`.** `tests
   /test_agent_registration.py::test_private_key_and_token_never_leak
   _during_registration` drives the full registration flow (registration,
   confirm via storage, challenge poll, token request) against a real
   fleet app over real TLS, recording every outgoing request via `httpx`'s
   own `event_hooks["request"]`, and asserts the private key's raw bytes
   (and its base64url encoding) never appear in any recorded request body,
   header, or log line, and that the finally-issued raw token never appears
   in a request (it is only ever *received*, in a response, during this
   flow) or a log line. `tests/test_agent_heartbeat_sender.py::test
   _private_key_and_raw_token_never_leak_into_logs_or_requests` covers the
   complementary case for that module: the token legitimately *is* sent,
   once, as the `Authorization` header of the one heartbeat request --
   what must never happen there is the token appearing a second time, in a
   request *body*, or in a log line.
5. **Untested non-2xx/edge branches**, closed with direct tests rather than
   accepted as gaps: `heartbeat_sender.py`'s flush-failure (`403` and a
   generic `500`), send-failure (`500`), and empty-buffer-file branches
   (all via `httpx.MockTransport`, now 100% covered); `registration.py`'s
   `register`-non-201, challenge-non-200, and token-non-200 branches --
   split out into two new, directly unit-testable helpers,
   `_submit_registration_request`/`_submit_token_request` (same behaviour,
   `register`'s own control flow unchanged, just easier to hit each branch
   without a real server) -- and the pathological-default fallback inside
   `_parse_and_clamp_retry_after` itself (now 100% covered).
   `transport.py`'s `_PinnedTransport.close()` was reachable (via
   `httpx.Client.close()` called outside a `with` block) but untested --
   added a direct test rather than a pragma. `_PinningNetworkBackend
   .connect_unix_socket` is marked `# pragma: no cover` with a reason: it is
   genuinely unreachable, since `build_client` never passes `uds=` to
   `httpcore.ConnectionPool` and nothing else in this module ever
   constructs a unix-socket URL.

**Verification after the fix commit** (fresh venv): `ruff check .` clean;
`mypy .` and `mypy protocol fleet agent tools` clean (70/41 files); full
suite `801 passed`, `TOTAL` coverage `99%` (3252 statements, 17 missed --
none of the 17 in `agent/`, all in `agent/loop.py`'s still-deferred
stage-2/24 command stubs, `fleet/admin.py`/`fleet/storage.py`'s own
pre-existing single missed lines, or `tools/check_image_config.py`'s CLI
entry point); the four P5.0 test files run five times in a row with no
flakiness.

Not touched by this fix round: the merge with `main` (11ff781: P3.4a,
`PROTOCOL_VERSION = 2`, P5.6 watchdog) brought in unrelated changes this
package's own docstrings and tests did not need to react to (P4.2b's
registration models already existed before the bump; nothing about this
package's own wire format changed).

## Agent transport: pinned HTTPS client, device registration, token storage,
heartbeat sending (P5.0, sections 3, 4, 5, 10, 14, 15.3, 18.1, 18.2, 19.5,
23.2) **SR**

**Scope, per the project owner's split (2026-09-26).** P2.3 ("agent:
collect and send heartbeat") was deferred whole because it depends on
thermoctl's still-missing `/api/v1/health` -- but only `collect_heartbeat`
(reading thermoctl) actually needs that endpoint. Everything else P2.3
named -- TLS pinning, sending, catch-up buffering -- does not, and is built
here as three new modules: `agent/transport.py`, `agent/registration.py`,
`agent/heartbeat_sender.py`. `agent/loop.py::collect_heartbeat` and the
main loop itself are untouched; `agent/loop.py::send_heartbeat` stays a
placeholder too, deliberately (its docstring now explains why: wiring it
up needs a client/apartment/buffer-path this module's own registration
flow would produce, which the main loop does not yet assemble).

### The pinned HTTPS client (`agent/transport.py`)

**Fingerprint format: `sha256:<hex>`** -- lowercase, exactly 64 hex
characters, the SHA-256 digest of the fleet server's **leaf** certificate's
raw DER bytes. `parse_certificate_fingerprint` rejects anything else
(wrong prefix, wrong length, non-hex, or uppercase hex -- rejected rather
than silently lowercased) instead of falling back to "no pin".
`fingerprint_for_certificate` is the inverse, used by every test that needs
to compute the pin for a certificate it just generated. `fleet/ui_routes.py`'s
`FLEET_CERT_FINGERPRINT` env var (the "Vorbereiten" page, P4.2) now
documents this same format in a comment -- the value itself was already
opaque there (passed straight into `agent-registration.json`, never parsed
fleet-side), so no behaviour changed, only the format is now written down
in one place both sides can point at.

**Two independent barriers, both always on:**

1. **Ordinary certificate verification, never disabled.** `ssl
   .create_default_context()` (or, for a test, `cafile=<throwaway CA>` --
   still verifying, just against a different trust root) -- `verify=False`
   appears nowhere in this module or in any test that exercises it, per the
   work order's explicit instruction.
2. **Fingerprint pinning**, as a genuinely separate check on top: the
   pinned value must equal the SHA-256 digest of whatever leaf certificate
   the live connection actually negotiates.

**What the pin check guarantees, precisely (investigated, not assumed).**
`httpx.HTTPTransport` exposes no hook between "TLS handshake completed" and
"request bytes written" -- it does not even expose `network_backend`, the
one `httpcore.ConnectionPool` constructor parameter that would allow one.
This package therefore builds its own minimal transport (`_PinnedTransport`,
mirroring `httpx.HTTPTransport`'s own `handle_request` translation, the one
part worth reusing verbatim) wired to a custom `httpcore.NetworkBackend`
(`_PinningNetworkBackend`) whose connections are wrapped in
`_PinCheckingStream`. `_PinCheckingStream.start_tls` performs the real
handshake (delegating to `httpcore`'s own `SyncBackend`), then immediately
reads the negotiated leaf certificate off the resulting stream
(`get_extra_info("ssl_object").getpeercert(True)` -- positional, not the
keyword form, since this is the low-level `_ssl._SSLSocket` object
`httpcore`'s `SyncBackend` itself hands back, not the higher-level `ssl
.SSLObject`) and compares its SHA-256 digest to the pinned value **before
returning that stream to its caller**. `httpcore.ConnectionPool`'s own
connection-establishment path calls `network_backend.connect_tcp(...)` and
then `stream.start_tls(...)` strictly *before* constructing the
`HTTP11Connection` object that would go on to write the request line,
headers (including `Authorization: Bearer ...`), or body. **A pin mismatch
therefore aborts before that object exists at all -- no request byte of any
kind is ever written on that connection before the pin has been checked
against the actual, live-negotiated certificate.** This is a guarantee
about bytes on the wire, not merely "the response is discarded" -- the
minimum the work order asked for is exceeded, not just met.
`tests/test_agent_transport.py::test_pin_mismatch_refuses_request_and_never_delivers_it`
and `tests/test_agent_heartbeat_sender.py::test_pin_mismatch_never_delivers_the_bearer_token`
both prove this directly with a recording TLS server: on mismatch, the
server's own handler never runs at all, so it records nothing -- not a
method, not a path, not one header.

On mismatch, `CertificateFingerprintMismatch` (a `httpx.TransportError`
subclass) is raised; the httpcore response is discarded, unread.

**Only `https://` is accepted** -- `build_client` checks the URL scheme
before attempting any connection at all (`InvalidFleetAddress`).

**A leaked pool slot, found and fixed while writing the tests.** The first
version of `_PinnedTransport.handle_request` read a response's body without
ever closing the underlying `httpcore` response stream -- `httpcore` only
returns a connection to its pool once that stream is explicitly closed, so
every request leaked a pool slot instead of reusing a keep-alive
connection. Invisible at low request volume, but a tight poll loop (the
registration test's own confirm-then-poll race) exhausted the default
`max_connections=10` within a couple of dozen calls and raised
`httpcore.PoolTimeout`. Fixed by closing the response stream in a
`finally` block; a regression here would show up immediately as a flaky
`PoolTimeout` in the registration/heartbeat test suites, not silently.

### Device registration (`agent/registration.py`)

Implements 15.3 steps 1-3 exactly as P4.2b defined the fleet side: read
`agent-registration.json` (default `/boot/firmware/agent-registration.json`,
the FAT32 boot partition, sections 15.3.1, 19.1, 19.5) -> generate or load
an Ed25519 key pair -> `POST /v1/registration` with the one-time code and
the **public** key -> compute and display the verification code
(`protocol.registration.verification_code_for`) -> poll
`.../{registration_id}/challenge` every 60 s (honouring the server's own
`Retry-After`, which `fleet.app.request_token_challenge` always sets to
`60`) until confirmed -> sign the exact domain-separated message
`fleet.app.request_device_token` verifies
(`b"thermoctl-fleet/token/v1\0" + registration_id + b"\0" + nonce"`) ->
fetch the token once -> store it -> stop.

**Idempotent:** an existing token file short-circuits everything else, no
network call at all -- proven by pointing a second `register()` call at a
certainly-unreachable address and confirming it never tries.

**File locations and modes** (`DEFAULT_DATA_DIR = /var/lib/thermoctl-agent`,
separate from the boot partition, which is world-readable on any computer
the card is plugged into):

| File | Mode | Content |
|---|---|---|
| `device_private_key.pem` | 0600 | Ed25519 private key, PKCS8 PEM, unencrypted -- **never transmitted, never logged**; only `.sign(...)` and, once, `.private_bytes(...)` to write this file, are ever called on it |
| `agent_token` | 0600 | the raw agent token, once issued |
| `registration_status` | 0644 | line-based (`status=...`, `verification_code=...` while waiting), the same convention as the watchdog's own state/health-report files (sections 17, 22.3) -- for a local component such as the section 23.2 "waiting for assignment" LED pattern to read without parsing a log line |

Both the private-key file and the token file are written via `os.open`
with the mode already set at creation (not `write_bytes` + a separate
`chmod`, which would leave a window at the umask's default mode); the data
directory itself is created at `0700`.

**Refuses anything unexpected from the server:** every response is
validated against its `protocol.registration` model
(`RegistrationAccepted`, `TokenChallenge`, `TokenIssued`) via
`model_validate` -- a response that does not fit raises
`pydantic.ValidationError`, uncaught, rather than being guessed at.

**`sleep=` is an injectable parameter, not a `time` module monkeypatch.**
The first version of the poll-loop test patched `agent.registration.time
.sleep` directly -- but `agent.registration`'s `time` **is** the process's
one shared `time` module (`import time` binds the same singleton), and
`httpcore`'s own connection pool also calls `network_backend.sleep(...)`
(which delegates to `time.sleep`) for unrelated internal bookkeeping. A
patched global `time.sleep` therefore also intercepted `httpcore`'s calls,
producing a confusing mix of the test's own fake delay and `httpcore`'s
real ones in the same recorded list. `register()`/`_poll_for_challenge`
instead take `sleep: Callable[[float], None] = time.sleep` as an ordinary
parameter, defaulting to the real thing in production; a test passes a
fast fake directly, scoped to exactly this call, nothing else in the
process.

### Heartbeat sending and buffering (`agent/heartbeat_sender.py`)

`send_heartbeat(client, heartbeat, apartment=..., buffer_path=...)`:

1. First flushes anything already buffered from an earlier outage via one
   `POST /v1/heartbeats` batch call (section 5's own "in one batch"), so a
   reconnect never sends a newer heartbeat ahead of the catch-up batch that
   should have arrived first.
2. Then sends `heartbeat` itself via `POST /v1/heartbeat`.
3. On any transport failure (unreachable server, pin mismatch, timeout) or
   a non-2xx/non-401/403 response, `heartbeat` is appended to a local,
   persisted buffer (a single JSON file, written atomically -- temporary
   file plus `Path.replace`, the same pattern `agent.loop
   .report_watchdog_state` uses) instead of being lost.

**Buffer cap: `protocol.heartbeat.MAX_CATCH_UP_HEARTBEATS` (240), oldest
dropped first** -- matches `POST /v1/heartbeats`'s own `Body(max_length=240)`
on the fleet side exactly, proven by feeding 245 heartbeats to a
permanently-unreachable client and checking the buffer holds exactly 240,
starting from the sixth one fed in.

**401/403 is never buffered** -- a token the fleet service refuses (revoked,
or valid for a different apartment) will not start being accepted again by
retrying with the same token later; `HeartbeatAuthError` is raised
immediately instead, so a caller can surface "token revoked" rather than
silently growing a buffer for a token that is never coming back. Proven
against a real fleet app by revoking a device's token (`Storage
.remove_device`) mid-test and confirming the next `send_heartbeat` call
raises without touching the buffer file.

**Never sends a heartbeat for a different apartment than the token's** --
checked locally (`HeartbeatApartmentMismatch`), before any network call at
all, as defense in depth on top of the fleet side's own identical check
(`fleet.app.receive_heartbeat`'s `authenticated_apartment !=
heartbeat.apartment`).

### Tests, all against real TLS -- no mock of TLS anywhere

`tests/tls_support.py` (not itself a test file) generates a throwaway CA
and leaf certificate pair with `cryptography` at test runtime (never
committed) and either starts the **real** `fleet.app.app` under a real
`uvicorn` server over that TLS (`run_tls_fleet_app`), or a minimal
`http.server`-based server that records every request it actually receives
(`run_recording_tls_server`, for the pin-mismatch-never-delivers-anything
proof). `tests/test_agent_transport.py` (14 tests), `tests
/test_agent_registration.py` (7 tests, including the full happy path with
the fleet app run in a background thread while a second, independent
`Storage` instance on the same sqlite file plays the landlord's confirm
action), and `tests/test_agent_heartbeat_sender.py` (7 tests) cover the
full acceptance list from the work order. All three files were run five
times in a row with no flakiness observed.

**Open points, explicitly not built here:** wiring `send_heartbeat`/
`register` into an actual running main loop (needs `collect_heartbeat`,
still deferred); the SSE command channel (P5.1) and everything downstream
of it in step 5; WireGuard (section 14's own key-pair generation is a
separate, not-yet-built concern -- this package only reuses section 14's
"private key never leaves the device" reasoning for the *registration*
key, not for a WireGuard tunnel).


## Cross-review hot fix, round 2: directories owned by root, and an agent with no access to the socket it was mounted for

**Two further findings on the same fix (round 1, entry directly below),
both reproduced by reading `docker/Dockerfile.agent` rather than assumed.**

**Finding 1: the tmpfiles.d directory was owned by root.** Round 1 shipped
`image/common/tmpfiles.d/thermoctl-agent.conf` as `d /run/thermoctl-agent
0755 root root -`. `docker/Dockerfile.agent` runs the agent as an
unprivileged, explicitly non-root user (`useradd --system --uid 10002
agent`, `USER agent`) -- a root-owned, mode-0755 directory gives that uid
read-and-execute access (list, traverse) but **no write permission at
all**. Every one of the agent's atomic writes into that directory (the
health report, the new LED status file) would therefore fail with
`EACCES`, silently undoing the entire point of round 1's directory-mount
fix: the directory would exist and be mountable, but the agent could never
actually write into it. **Fix:** `docker/Dockerfile.agent` now pins the
group explicitly too (`groupadd --system --gid 10002 agent`, then
`useradd ... --uid 10002 --gid 10002 agent` -- previously only the uid was
pinned, the gid was whatever `useradd` picked on its own, which is not
guaranteed stable release to release); `thermoctl-agent.conf`'s entry is
now `d /run/thermoctl-agent 0755 10002 10002 -`, and its own comment cross-
references the Dockerfile line and explains *why* a plain name would not
have worked (no `agent` user/group exists on the *host*, only inside the
container -- the numeric id is the actual, and only, shared contract).
`/var/lib/thermoctl-watchdog` needs the identical ownership for the
identical reason (the agent writes `state.env` into it too) but has no
tmpfiles.d entry of its own (created once at image build time, not
recreated per boot, section 17 "Fallback without a proven revision") --
that requirement is now a documented TODO in `image/common/README.md`
instead, so the still-unimplemented build step (section 19.4) does not
have to rediscover it the way this cross-review did.
`tools/check_image_config.py::check_tmpfiles_entry` now parses the actual
`d <path> <mode> <uid> <gid> <age>` line structurally (not a substring
match, which could pass on "10002" appearing in the wrong field or a
comment while the real uid/gid still said "root") and rejects anything
other than `10002:10002`; four new tests in `tests/test_image_config.py`
(owned by root, mismatched gid, too few fields, the correct ownership
passing).

**Finding 2: the agent had no access to the Docker socket it was mounted
for.** `image/common/agent-compose.yml` bind-mounts `/var/run/docker.sock`
so the agent can reconcile the other three services (section 13) -- but
the socket is owned `root:docker 0660` on the host, and the agent's own
uid 10002 is not a member of that group by default, and cannot be made one
by anything baked into the *agent's own container image* (group
membership for a host-socket permission has to come from the host, at
runtime, not from `docker/Dockerfile.agent`). Without it, `agent-compose
.yml`'s existing Docker-socket mount would have been present but useless:
the agent could open the socket file (it is bind-mounted, so the path
exists inside the container) but every actual call against it would be
refused by the daemon's own permission check on the connecting process's
group membership. **Fix, the simplest robust variant that keeps nothing
per-host hard-coded in the repository:** `agent-compose.yml`'s `agent`
service now declares `group_add: ["${DOCKER_GID:?...}"]` -- Compose's own
supplementary-group mechanism, which adds the named *numeric* gid to the
container's process without changing its uid away from 10002 or otherwise
touching `docker/Dockerfile.agent`. The gid itself is intentionally left
unresolved in the repository (`${DOCKER_GID:?message}` fails the `docker
compose` invocation loud and immediately if unset, the same "no digest, no
start" reasoning security principle 2 already applies elsewhere) -- the
"docker" group's gid is whatever the image build's own package install
assigned it (system-allocated, not a value this repository could pin
without risking a silent mismatch against the real host), so it is
resolved exactly once, by the still-unimplemented image build step
(section 19.4), via `getent group docker | cut -d: -f3`, and written to
`/etc/thermoctl-agent/.env` (`DOCKER_GID=<gid>`) -- the same directory as
`compose.yml` itself, which `docker compose` reads a `.env` file from
automatically, needing no change to how the watchdog invokes it
(`watchdog/runtime.go`'s `cliRuntime.Start` passes no extra flag for
this). Documented as a new TODO bullet in `image/common/README.md`,
alongside the ownership one above. `tools/check_image_config
.py::check_agent_compose_file` gained two more required substrings
(`"group_add:"`, `"${DOCKER_GID:?"`), plus one new test asserting a
compose file with every other requirement met but no `group_add` is still
rejected. The security meaning is noted directly in the compose file's own
comment: membership in the host's "docker" group is, in practice,
equivalent to root on the host, bounded only by security principles 2 and
5 -- the same bound the socket mount's own comment already stated, not
widened by this fix, only made *usable* by an unprivileged container in
the first place.

**Cosmetic corrections, same pass:** round 1's entry (and
`image/common/agent-compose.yml`'s own comment) cited "section 22.5" for
the image-build-time state file guarantee; the actual source is section
17's "Fallback without a proven revision" subsection (22.5 does not exist
as a citation for this -- corrected in both places; the older,
pre-existing citations of the same "22.5" mistake in `watchdog/state.go`,
`watchdog/watch.go`, and `watchdog/README.md` predate this task and are
out of scope for it, left alone rather than touched incidentally). Round
1's own STATUS.md text said "six new tests" where it was actually four;
corrected there, with a forward pointer to the further tests this round
adds.

**Verification (fresh venv, `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"`):** `ruff check .`, `mypy .` (61 source files),
`mypy protocol fleet agent tools` (38 source files) all clean; `python -m
tools.check_image_config` passes; `python -m pytest -W
ignore::ResourceWarning` **744 passed**, TOTAL 2963 stmts / 21 miss = 99%
overall coverage (the two remaining uncovered spots,
`tools/check_image_config.py` lines 107 and 241-247, are the same
pre-existing, unrelated gaps as both entries above --
`check_watchdog_unit`'s untested branch and `main()`'s own body; every new
assertion added across both rounds, including `check_tmpfiles_entry`'s
ownership check, its too-few-fields guard, and `check_agent_compose_file`'s
`group_add` check, is covered by its own dedicated test). `watchdog/`: this round
touched no Go file at all (`docker/Dockerfile.agent`,
`image/common/agent-compose.yml`, `image/common/tmpfiles.d
/thermoctl-agent.conf`, `image/common/README.md`, `tools
/check_image_config.py`, `tests/test_image_config.py`, `docs/STATUS.md`
only) -- `go vet ./...` clean, `go test -count=1 ./...` green across all
three packages (run three times in a row, no flake), `gofmt -l .` empty,
`bash check_contract.sh` passes, `grep -c require go.mod` still `0`. Line
counts unchanged from round 1: watchdog's own six files **280/300**;
`cmd/thermoctl-leds` + `internal/ledsysfs` **353** combined.

## Cross-review hot fix: P5.7's status file, and P5.6's compose file before it, bind-mounted single files instead of directories

**The finding, stated plainly.** P5.7's own cross-review (2026-09-27)
found that `/run/thermoctl-agent-led-status.env` (the new agent-written
status file) was never mounted into the agent container at all in
`image/common/agent-compose.yml` -- the agent would have written it into
the container's own private `/run`, the host (and therefore
`cmd/thermoctl-leds`, running on the host) would never see it, and every
device would have permanently shown LED 1 slow-blinking ("not yet
healthy") and LED 2 slow-blinking ("open fault"), regardless of the
agent's real state. Chasing that finding to its root cause surfaced a
second, wider one already sitting on `main`, from P5.6: the compose file's
*existing* two volumes, `/var/lib/thermoctl-watchdog/state.env` and
`/run/thermoctl-agent-health.env`, bind-mount **individual files**, not the
directories that hold them. `agent.loop.report_watchdog_state` and
`report_health` (P5.6) -- and `report_led_status` (P5.7) -- all write
these files the same way: a temporary file next to the target, then
`Path.replace` (a thin wrapper over `rename(2)`). A rename only ever
succeeds within the filesystem/directory it started in. With the target
bind-mounted as a single file, the temporary file the agent creates next
to it lives on the *container's own* private overlay filesystem -- so the
rename either fails outright (`EBUSY`, a single-file bind mount cannot be
replaced by renaming a different inode onto it) or, depending on the
container runtime and kernel, "succeeds" locally and silently detaches the
mount, after which the host keeps looking at whatever snapshot happened to
be there when the container started -- forever. Where the host file did
not exist yet at all, Docker's own bind-mount behaviour additionally
creates a **directory** at that path instead of a file, which would have
made the agent's very first write fail outright with `IsADirectoryError`.
In short: the state file and health report were *already* broken by this
bug on `main` before this task started (P5.6's own tests never exercise
the real compose file end to end, only the file format each side reads
and writes -- exactly the gap a cross-review, not a unit test, is for),
and P5.7's new file would have shipped with the identical bug on day one.

**The fix: mount the two directories that hold these files, not the files
themselves.** `image/common/agent-compose.yml` now bind-mounts:

- `/var/lib/thermoctl-watchdog:/var/lib/thermoctl-watchdog` (was
  `.../state.env:.../state.env`) -- persistent, the directory (and its
  `state.env`) already exists from image build time (section 17,
  "Fallback without a proven revision"); this mount only exposes it to the
  container too.
- `/run/thermoctl-agent:/run/thermoctl-agent` (was
  `/run/thermoctl-agent-health.env:/run/thermoctl-agent-health.env`) --
  a new shared directory for **both** `/run`-resident files: the health
  report (now `/run/thermoctl-agent/health.env`, was
  `/run/thermoctl-agent-health.env`) and the new agent status file (now
  `/run/thermoctl-agent/led-status.env`, was
  `/run/thermoctl-agent-led-status.env`). One directory for both rather
  than two, since both are tmpfs-resident, wiped-on-reboot, agent-written
  live status files with the identical "must not survive as a stale
  snapshot" reasoning (section 22.3) -- no reason to give them two mounts
  where one does the same job.
- `/var/lib/thermoctl-agent:/var/lib/thermoctl-agent` (unchanged --
  already a directory mount, never affected by this bug).

**A directory that does not yet exist on the host at boot time, for the
`/run/` one:** new `image/common/tmpfiles.d/thermoctl-agent.conf`, a
`systemd-tmpfiles` snippet installed as `/etc/tmpfiles.d/thermoctl-agent
.conf` during image preparation (section 19.3) and applied by
`systemd-tmpfiles-setup.service` during early boot, before `docker.service`
starts any container -- the directory must exist on the host before the
compose file's bind mount can attach to it. `/var/lib/thermoctl-watchdog`
needs no such entry: it is persistent (`/var/lib`, not tmpfs) and already
created once at image build time, per section 17 ("Fallback without a
proven revision"). **Ownership: see the round-2 hot-fix entry directly
below** -- this entry originally shipped the tmpfiles.d line as `d
/run/thermoctl-agent 0755 root root -`, which a same-day re-review caught
as wrong before this branch was ever merged.

**Every default path updated to match:**
`watchdog/cmd/thermoctl-leds/main.go`'s own flag defaults --
`-health-file` from `/run/thermoctl-agent-health.env` to
`/run/thermoctl-agent/health.env`, `-agent-status-file` from
`/run/thermoctl-agent-led-status.env` to
`/run/thermoctl-agent/led-status.env`; `-state-file` unchanged
(`/var/lib/thermoctl-watchdog/state.env` -- only the *mount*, not the
path, changed for that one). `watchdog/main.go` itself hard-codes no
default paths (both its `-file`/`-health-file` flags default to `""`, set
only via the systemd unit's `ExecStart`), so only
`watchdog/thermoctl-watchdog.service` needed its `-health-file` argument
updated the same way. `watchdog/cmd/thermoctl-leds/thermoctl-leds.service`
passes no path flags at all (relies on `main.go`'s own defaults), so it
needed no change. **Watchdog line count: unchanged at 280/300** -- only a
string literal and a comment changed in `cmd/thermoctl-leds/main.go`,
which is not one of the watchdog's own six counted files, and no line was
added to or removed from any of those six.

**`tools/check_image_config.py`, extended, not just documentation:**
`check_agent_compose_file` now also asserts the two directory-mount lines
are present *and* that the two old single-file-mount lines are **absent**
-- checked both ways so a partial revert (the directory lines added back
in without also removing the file-level ones, or vice versa) is still
caught, not just a wholesale one. New `check_tmpfiles_entry` asserts
`image/common/tmpfiles.d/thermoctl-agent.conf` exists and actually
mentions `/run/thermoctl-agent` -- without it, the directory mount above
has nothing to attach to before the agent container starts. Both wired
into `check_all`. Four new tests in `tests/test_image_config.py`: directory
mounts missing (rejected even with every other required substring
present), single-file mounts present alongside the correct directory
mounts (still rejected -- a partial revert), tmpfiles entry missing,
tmpfiles entry present but naming the wrong directory. (Three more tests,
for ownership, are added by the round-2 entry below.)

**Python: a new test proving the invariant the whole fix depends on**,
not just asserting the fix from the outside. `tests/test_watchdog_contract
.py::test_atomic_writes_stay_within_the_target_directory` monkeypatches
`pathlib.Path.replace` to record, for all three writers
(`report_watchdog_state`, `report_health`, `report_led_status`), the
parent directory of both the rename's source (the temp file) and its
target (the real file) -- and asserts they are identical for every call.
This is precisely the property a directory-level bind mount requires and
a file-level one cannot provide: `rename(2)` only ever succeeds within one
filesystem, so as long as the temp file and its target share a parent
directory, mounting that one directory into the container is sufficient;
if either writer ever changed to build its temp file's path differently
(e.g. under a shared `/tmp`), this test would fail immediately rather than
the bug resurfacing silently on a real device.

**Why a unit test did not already catch this, and what would have:** every
existing test (`tests/test_watchdog_contract.py`,
`watchdog/check_contract.sh`) exercises the file format and the read/write
contract directly against a plain temp directory -- correctly, since that
directory is never bind-mounted in a test, single-file-mount-vs-directory
-mount is not a distinction a test without a real Docker container can
observe at all. `tools/check_image_config.py` was the right layer to add
the missing check to instead: it already is the tool that reads
`agent-compose.yml` as data and asserts properties about it (P5.6's own
`check_agent_compose_file`), and cross-review -- reading the actual
compose file end to end against what the agent actually does when it
writes -- is what surfaced this, not a test that only exercises one side
of the contract in isolation. This is the documented "no substitute for an
actual build" gap `tools/check_image_config.py`'s own module docstring
already names: a real end-to-end run (agent container actually writing
through the actual bind mount) would need `image.yml`'s eventual real
build (section 19.4, still not implemented, see `image/README.md`'s "State
of this scaffold"), not this scaffold's plausibility check -- which now at
least catches the specific regression this fix corrects.

**Verification (fresh venv, `python3.13 -m venv`, `pip install -e
".[dev,fleet,agent]"`):** `ruff check .`, `mypy .` (61 source files),
`mypy protocol fleet agent tools` (38 source files) all clean; `python -m
tools.check_image_config` passes; `python -m pytest -W
ignore::ResourceWarning` **739 passed**, TOTAL 2955 stmts / 21 miss = 99%
overall coverage (the newly-listed uncovered lines in
`tools/check_image_config.py`, 107 and 194-200, are `check_watchdog_unit`'s
pre-existing untested branch and `main()`'s own body -- the same
pre-existing, unrelated gaps as before this hot fix, not the new
`check_agent_compose_file`/`check_tmpfiles_entry` assertions, which are
each directly covered by their own new test). `watchdog/`: `go vet ./...`
clean, `go test -count=1 ./...` green across all three packages (run three
times in a row, no flake), `gofmt -l .` empty, `bash check_contract.sh`
passes (rebuilt against the corrected default paths), `grep -c require
go.mod` still `0`. Line counts: watchdog's own six files unchanged at
**280/300**; `cmd/thermoctl-leds` + `internal/ledsysfs` unchanged at
**353** combined (not counted against the watchdog's budget).

## P5.7 -- status LEDs as a separate program next to the watchdog (section 23)

**Decision by the project owner, 2026-09-26 (do not re-open, see the task's
own work order):** the two status LEDs are driven by a **separate, small
Go program**, `watchdog/cmd/thermoctl-leds/`, in the same module as the
watchdog but its own `go build` target -- **not** by the watchdog itself
as the implementation plan originally had it (P5.6's own entry above,
"P5.7 is next..."). Two reasons, both already in
`docs/specification.md` section 23's own "Decided afterward" paragraph:
P5.6's cross-review left the watchdog only **one line of headroom** before
its 300-line budget (section 18.3), and -- the more important one -- a bug
in LED-driving code must never be able to reach the one process whose job
is swapping and rolling back the agent reliably.

**Watchdog line count: went down, not just "did not grow".**
`watchdog/leds.go`/`leds_test.go` (the still-unimplemented `LedSetPattern`
stub from the scaffold) are gone from the watchdog's own package --
**moved**, not duplicated, into `watchdog/internal/ledsysfs`
(`LedPresent`, `ApplyPattern`, now actually implemented against the
kernel `timer` trigger, section 23.1). The watchdog's own six production
files (`main.go`, `watch.go`, `state.go`, `health.go`, `linefile.go`,
`runtime.go` -- `leds.go` no longer one of them) are unchanged in content
and went from **299 to 280 statement lines** purely by that removal,
measured with the same command as P5.6's own count: `grep -v '^\s*//' <file>
| grep -v '^\s*$' | wc -l`, summed. `go vet ./...` clean, `go test
-count=1 ./...` green, `gofmt -l .` empty, `grep -c require go.mod` still
`0`, `check_contract.sh` still passes (extended, see below).

**`cmd/thermoctl-leds/` itself: 288 statement lines** across `main.go`
(31), `loop.go` (65), `inputs.go` (130, after factoring the four
`loadXxx` functions through a generic `loadOptional[T any]` the same way
`watchdog/linefile.go::openAndParse` already does for the watchdog's own
`LoadState`/`ReadHealth`), `decide.go` (62); plus `internal/ledsysfs`'s
own **65** statement lines. None of this counts against the watchdog's
300-line budget -- it is a separate binary, built with its own `go build
./cmd/thermoctl-leds`, and the task set no line limit for it beyond
"small", which this is, given four input file formats, precedence rules,
and a staleness check to implement.

**Inputs, all local files, no network (section 23.1's own "no single
dependency" point applied here too):**

1. The watchdog's own state file (`desired`, `since`) and health report
   (`timestamp`, `digest`) -- unchanged formats, re-parsed by this
   program's own small key-value-line reader (duplicated from
   `watchdog/linefile.go`, not imported: `state.go`/`health.go` live in
   `package main` at the watchdog's module root, and a `main` package
   cannot be imported by a second program in the same module).
2. P5.0's `registration_status` file (`agent/registration.py
   ::_write_status`, branch `p5.0-agent-transport`, read there since main
   does not have it yet): `status=waiting_for_assignment` drives LED 1's
   fast blink (section 15.3's verification code step); any other value,
   or the file's absence, does not.
3. **New: the agent-written status file** for the three things only the
   agent loop can know -- `agent.loop.report_led_status` (new function,
   this task) writes it, line-based like the other three:
   ```
   timestamp=<unix seconds>
   cloud_contact=ok|lost
   fault=none|open
   control=ok|stalled
   ```
   No fixed path is hard-coded in `report_led_status` itself (same rule as
   `report_watchdog_state`/`report_health`); `cmd/thermoctl-leds`'s own
   `-agent-status-file` flag defaults to `/run/thermoctl-agent/led-
   status.env` -- under `/run/`, like the health report, because it is a
   live status snapshot, not persisted state that should survive a reboot
   stale. **Corrected by the hot-fix entry below** (originally
   `/run/thermoctl-agent-led-status.env`, a single file rather than a path
   inside the shared `/run/thermoctl-agent/` directory -- see that entry
   for why this had to change before it ever reached a real device).

**Staleness (documented threshold: 3x the heartbeat interval, 120s ->
360s, `cmd/thermoctl-leds`'s own `-stale-after` flag, overridable):**
applies to the **two periodic reports** (health report, agent status
file) via their own embedded `timestamp`, exactly the way
`watchdog/watch.go::AwaitHealthReport` already reads the health report's
timestamp against a deadline -- an aged-out report is treated as unknown,
never as "still good": LED 1 falls back to slow blink ("not yet healthy"),
LED 2 to slow blink as well (reusing its own "open fault" pattern as the
more cautious of its three defined ones, since section 23.2 defines no
fourth "unknown" pattern for LED 2 and falling back to "off" would itself
read as a stale "no fault"). The watchdog's state file and P5.0's
registration status file are **not** subject to this window: both change
only on real transitions (a new desired digest; registered/assigned), not
on a periodic cadence, so there is no heartbeat interval to measure their
age against -- a device can sit in "waiting for assignment" for days
without a fresh write and must still show the fast-blink pattern.

**Precedence, most specific first:**

- LED 1 (device, green): waiting-for-assignment overrides everything ->
  agent not healthy (state/health missing, stale, wrong digest, or a
  health report that predates the current desired revision, section
  22.3's own "an older report does not count" read the same way here) or
  agent-status unknown -> slow blink -> healthy but `cloud_contact=lost`
  -> two short blinks -> otherwise steady on. "Agent healthy" is defined
  identically to `AwaitHealthReport`'s own success condition
  (`health.Digest == state.Desired && health.Timestamp >= state.Since`),
  independently re-derived from the same two files rather than reading
  the watchdog's in-memory `Outcome` (which is never persisted to disk,
  and therefore not a "local file" input in the task's own sense).
- LED 2 (system, yellow): agent-status unknown -> slow blink -> control
  stalled -> steady on (ranked above a merely open fault, the more severe
  condition) -> open fault -> slow blink -> otherwise off. Independent of
  LED 1's registration/health state -- section 23.2's own table for LED 2
  draws no such distinction.

**"Two short blinks, pause" is an approximation, documented, not an
oversight:** the kernel `timer` trigger exposes exactly one on/off period,
not a sequence, so a true grouped double-flash would need either a
software blink loop in this program (defeating the very reason section
23.1 chose the kernel trigger: the display keeps blinking even if this
program is briefly delayed) or the kernel's separate `pattern` trigger,
whose presence is not guaranteed the way `timer`'s is. Implemented instead
as a distinct rhythm (100ms on, 700ms off) that is clearly different by
ear and eye from the continuous, even "fast blink" (100ms on, 100ms off,
"waiting for assignment") -- `internal/ledsysfs`'s own test proves all
three timer-driven patterns render distinguishable periods.

**Missing LED driver (section 23.3, "Raspberry Pi only"):** checked once
at startup (`ledPresentEither`), not on every poll -- if neither of the
two sysfs brightness files exists, the program logs one line and exits
cleanly (exit 0); `thermoctl-leds.service` uses `Restart=on-failure`
rather than `Restart=always` so that clean exit is not treated as a crash
worth restarting. A per-LED check (`ledsysfs.ApplyPattern`'s own
`LedPresent` guard) still covers the case of only one of the two files
existing.

**Tests:** Go -- `internal/ledsysfs`: LED present/missing, unknown
pattern rejected, off/steady-on write the right trigger+brightness, all
three timer patterns write "timer" plus pairwise-distinct delay pairs.
`cmd/thermoctl-leds`: every state of section 23.2's two tables via table
tests (`decide_test.go`), precedence (waiting-for-assignment overriding
health; control-stalled outranking open-fault), staleness (injected
clock, `testNow`/`time.Unix` fixtures, no real sleeping), missing input
files (`inputs_test.go`, `loadOptional` returns `nil, nil`), missing LED
driver end to end (`loop_test.go`), a full healthy-device pass writing
real sysfs file contents, and `-check-mode`'s own uppercase-key output
(mirroring `watchdog/main.go::runCheckMode`). Python --
`tests/test_watchdog_contract.py`: `report_led_status` writes all four
fields, atomic write (no leftover `.tmp`), overwrite semantics.
`watchdog/check_contract.sh` extended: Python writes the new file,
`thermoctl-leds -check-mode` reads it back, values compared -- the same
cross-language sequence as the state/health files, now covering three
files and two Go binaries. `tools/check_image_config.py` gained
`check_leds_unit` (mirrors `check_watchdog_unit`), `image/README.md`,
`image/common/README.md`, `image/pi/README.md` note the second unit
copied at build time next to the watchdog's; `image/x86/README.md` notes
it is deliberately *not* enabled there (no 40-pin header, section 23.3).

**CI (`.github/workflows/go.yml`):** the `build` job now also builds
`cmd/thermoctl-leds` for `amd64`/`arm64` (`CGO_ENABLED=0`, static, with a
checksum, same as the watchdog binary) and uploads it as its own
artifact; `contract-test` builds both binaries via the extended
`check_contract.sh`. No trigger paths changed (`watchdog/**` already
covers the new `cmd/`/`internal/` subdirectories).

**Verification:** `watchdog/`: `go vet ./...` clean, `go test -count=1
./...` green across all three packages, `gofmt -l .` empty, `bash
check_contract.sh` passes, `grep -c require go.mod` is `0`. Cross-compiled
both binaries for `linux/amd64` and `linux/arm64` with `CGO_ENABLED=0`
directly (not only via CI) to confirm the build step CI will run.
Root, fresh `python3.13 -m venv` + `pip install -e ".[dev,fleet,agent]"`:
`ruff check .` clean, `mypy .` clean (61 source files), `python -m
tools.check_image_config` passes, `python -m pytest -W
ignore::ResourceWarning` **734 passed**, 99% overall coverage (the two
newly-uncovered lines in `tools/check_image_config.py`, `main()`'s own
body and one pre-existing branch of `check_watchdog_unit`, are unrelated
pre-existing gaps, not introduced by this task -- `main()` was already
only exercised as an entry point, `# pragma: no cover`'d at its `if
__name__` guard, same as before).

## Confirm/remove race test flake: a legitimate third interleaving, not a race bug (main session)

**Symptom.** `tests/test_device_lifecycle_registration_integration.py
::test_confirm_device_replace_previous_races_a_concurrent_remove_device`
(from the "Cross-review integration: P4.2 x P4.3" section below) failed
intermittently -- reproduced at 11/500 (~2.2%) isolated runs of the exact
scenario, in the same order of magnitude as the reported ~2/30 -- with
`AssertionError: assert ['confirmed', 'removed'] in (['confirm_failed',
'removed'], ['confirmed', 'remove_failed'])`: both the concurrent
`confirm_device(replace_previous=True)` for a new device and `remove_device`
of the apartment's current device succeeded, an outcome the test's own
fixed two-outcome-label assertion forbade outright.

**Diagnosis: (a), a legitimate interleaving the test forbade too strictly --
not a race bug.** `Storage.remove_device` and `Storage.confirm_device`'s
`replace_previous` path already race safely against each other over the
*same* assignment row (the guarded `UPDATE ... WHERE id = <row> AND
ended_at IS NULL` each uses). What the old assertion missed is a third,
equally legitimate interleaving: `remove_device` can commit its **entire**
transaction (closing the old assignment, moving the old device to its
target state, revoking the token, three audit rows) before
`confirm_device`'s own phase 2 ever reads the previous assignment at all.
`confirm_device` then correctly finds **no** open assignment to replace (it
was already closed) and proceeds exactly like an initial-commissioning
confirm -- creating a fresh assignment for the new device outright. Both
calls report success, and every invariant the test actually cares about
still holds: the old assignment closed **exactly once** (never twice), the
old device ends up in **the remover's** chosen target state (`faulty`),
never overwritten by `confirm_device`'s own choice (`in_storage`), the
apartment's token is revoked exactly once, the apartment ends with exactly
one open assignment (the new device), the new device is `in_service` with a
confirmed, non-invalidated registration, and no audit row claims a change
that never happened. Verified directly across 500 runs of the raw scenario
outside pytest (11 hits, `outcome distribution: {('confirm_failed',
'removed'): 484, ('confirmed', 'remove_failed'): 5, ('confirmed',
'removed'): 11}`), every one of the 11 "both succeed" hits showing
identical, fully consistent state (`current_assignment=sn-new
old_state=faulty new_state=in_service token_hash=None`, one `"closed"` row
for the old assignment, one `"assigned"` row for the new one, zero
`"closed"` rows for the new one).

**Fix: the test now asserts invariants for all three legitimate
interleavings, not two fixed outcome labels**
(`_assert_confirm_remove_race_invariants`,
`tests/test_device_lifecycle_registration_integration.py`), plus two new
deterministic tests that force each interleaving explicitly instead of
relying on real-thread timing to hit the rare one:
`test_confirm_device_replace_previous_forced_remove_wins_during_phase_gap`
(monkeypatches `hmac.compare_digest` -- the last call `confirm_device`'s
phase 1 makes -- to run `remove_device` to completion as a side effect
before returning the real comparison result, injecting the pause exactly at
the phase 1/phase 2 boundary the P4.2 reviewer's own technique used
elsewhere in this file) and
`test_confirm_device_replace_previous_forced_confirm_wins_first` (plain
sequential calls, no injection needed).

**A fourth case, found while forcing "confirm fully before remove" -- first
called a benign contract detail, then reclassified by the project owner as
a real defect and fixed (main session, follow-up, 2026-09-27).** Once
`confirm_device` has fully committed, the old device's assignment is no
longer open at all; `remove_device`'s own lookup of "the apartment's
currently open assignment" (section 20.2) then had no notion of *which*
device the caller had actually seen, and would silently act on the
apartment's now-current assignment -- the *new* device -- instead: setting
a device the landlord never saw to `faulty`/`in_storage` and revoking the
token it had just obtained. Concretely: the landlord opens "Gerät
ausbauen" for apartment X while device OLD is shown; meanwhile a confirm
assigns NEW; the landlord's stale submit then removes NEW. Initially
argued (see the paragraph this replaces, still visible in git history) as
"not a race by that point, not a corruption, the same class of UI-
staleness this codebase already accepts for a plain double removal" -- the
project owner's own read is sharper: a plain double removal ("remove
whatever is open") and this case ("remove *this specific* device") are not
the same class at all, since the landlord's form was rendered against a
*specific* assignment, and section 20.3's own "every change to assignment,
state, or token is logged: who, when, why" implies the "who" acted on what
they actually saw, not on whatever the row happens to mean by the time the
request lands.

**Fix: `Storage.remove_device` takes a new required
`expected_assignment_id: int` parameter and acts only on that exact
assignment, never on "whatever is currently open for this apartment
id".** Looks up the apartment's actual current open assignment first (as
before), then requires it to still have `id == expected_assignment_id`
before doing anything else; a mismatch -- already closed, or the apartment
now has a *different* open assignment, or the id names some other
apartment's assignment entirely (a tampered hidden field) -- is refused
with `"Die Zuordnung hat sich inzwischen geändert -- bitte neu laden."`,
before any write, no audit row (`fleet/storage.py`). The concurrent-
double-removal guarded `UPDATE` (`tests/test_storage.py
::test_remove_device_concurrent_double_removal_only_one_wins`) now also
filters on `expected_assignment_id`/`apartment_id`, unchanged in effect
since both racing calls already agree on the same id in that test.

**Carried end-to-end through the UI, not only the storage layer:**
`fleet.ui_inventory.ReplaceDeviceView` gained `current_assignment_id`
(from `Storage.get_current_assignment`, the same call that already builds
the rest of the form); `fleet/templates/ui/inventory_replace_device.html`
carries it as a hidden `expected_assignment_id` field; `fleet/ui_routes.py
::replace_device_submit` takes it as a required `Form(...)` int and passes
it straight through to `Storage.remove_device`, surfacing a mismatch as the
same `400` re-render every other validation error already uses.

**Proven directly, not only argued:**
`tests/test_storage.py::test_remove_device_rejects_a_stale_expected_
assignment_id` (the device gets replaced from under a stale
`expected_assignment_id`; refused, nothing touched, no audit row),
`::test_remove_device_rejects_an_expected_assignment_id_already_closed`
(the simpler already-closed variant), and `::test_remove_device_rejects_
an_expected_assignment_id_from_another_apartment` (tamper case: a *still
open* assignment belonging to a *different* apartment is refused, because
the lookup is always "this apartment's current open assignment first,
then compare id", never "does this id exist and is it open anywhere").
`tests/test_ui_inventory.py::test_replace_device_submit_stale_form_is_
refused` reproduces the landlord's exact scenario at the HTTP level: a
form is opened, a real `confirm_device(..., replace_previous=True)` call
replaces the device while it is still open, the stale submit gets `400`
with the message above, and the replacement device is untouched (still
`in_service`, exactly one `token_revoked` audit row -- confirm's own, not
a second one from the refused removal).

`tests/test_device_lifecycle_registration_integration.py
::test_confirm_device_replace_previous_forced_confirm_wins_first` (the
test that used to prove the old, now-rejected behaviour) is rewritten to
prove the fix instead: `remove_device` is now called with `sn-old`'s
*original* assignment id (captured once, before `confirm_device` ever
runs -- exactly what a landlord's already-open page would still carry,
having no way to know a replacement was confirmed in the meantime), and is
refused; `sn-new` ends the test exactly as `confirm_device` left it
(`in_service`, its own assignment still open, exactly one token
revocation, no audit row from the refused call).
`_assert_confirm_remove_race_invariants` (the three-interleaving helper
covering the real thread race) is otherwise unchanged: with
`expected_assignment_id` now pinned to the assignment the caller actually
saw, the case that used to silently corrupt state now simply folds into
the already-covered `("confirmed", "remove_failed")` outcome --
`remove_device` failing cleanly, having written nothing, looks identical
regardless of which of the two now-possible reasons (lost the race on the
same row, or the assignment moved out from under it entirely) tripped its
guard.

**A real, if minor, invariant bug found and fixed along the way, unrelated
to the race itself: `Storage.remove_device`'s own `token_revoked` audit row
hard-coded `before={"token_hash": "set"}`, regardless of whether the
apartment actually had a token.** `confirm_device`'s own token-revoke audit
row already computes this correctly (`had_token = apartment.token_hash is
not None`); `remove_device` did not, and this test's own fixture (an
apartment created via `_make_apartment`, never given a token) exposed it
directly -- a false audit claim ("a token was revoked") for an apartment
that never had one, exactly the "audit rows claiming actions that did not
happen" invariant this investigation was asked to check. Fixed in
`fleet/storage.py`'s `remove_device` to mirror `confirm_device`'s own
pattern; no existing test asserted the literal hard-coded value, so nothing
else needed updating.

**Sibling race tests checked for the same outcome-label-under-real-thread-
race weakness, none needed hardening:**
`tests/test_device_lifecycle_registration_integration.py
::test_concurrent_confirm_racing_manual_reported_to_faulty_exactly_one_
outcome` already asserts invariants conditionally, not fixed outcome
labels. `tests/test_device_registration.py
::test_confirm_device_concurrent_confirms_of_two_devices_for_one_apartment_
exactly_one_wins` and `::test_confirm_device_concurrent_confirms_of_the_
same_device_exactly_one_wins` assert `sorted(...) == [False, True]`, but
unlike this package's own race, their "exactly one wins" is enforced
structurally (the partial unique index on `assignments.apartment_id`, and a
guarded `UPDATE ... WHERE state = 'reported'` against the identical device
row for both threads) -- there is no code path by which a legitimate third
outcome could occur, so the fixed assertion is not flaky by construction.
`test_record_device_report_two_threads_same_code_exactly_one_wins` and
`test_confirm_device_wrong_attempts_counter_atomic_under_concurrency` are
likewise backed by a single atomically-guarded write each, not a
race-with-multiple-legitimate-outcomes. All four re-run 50x with no flake.

**Verification, initial round** (fresh venv, `python3.13 -m venv`, `pip
install -e ".[dev,fleet,agent]"` -- SQLAlchemy 2.1.1, mypy 2.3.1): `ruff
check .`, `mypy .`, `mypy protocol fleet agent tools` all clean; `python -m
pytest -W ignore::ResourceWarning` (732 passed, 99% coverage overall) run
3x with identical counts; the real-thread race test re-run 200x (0
failures, down from ~2.2%/500 measured before the fix -- the fix did not
change timing, only the assertion, so the *interleaving* still occurs at
the same rate, it is simply no longer treated as a failure); both new
deterministic tests and the two `test_device_registration.py` siblings
each re-run 50x (0 failures).

**Verification, follow-up round (`expected_assignment_id` fix, same fresh
venv):** `ruff check .`, `mypy .`, `mypy protocol fleet agent tools` all
clean; `python -m pytest -W ignore::ResourceWarning` run 3x -- **736
passed** each time, 99% coverage overall, every touched file
(`fleet/storage.py`, `fleet/ui_inventory.py`, `fleet/ui_routes.py`) at
100%; `TOTAL` missed-statement count was 21/21/22 across the three runs --
the one-statement wobble is `fleet/storage.py:2271`
(`confirm_device`'s own guarded-`UPDATE`-lost-the-race branch, pre-existing
code from the P4.2 x P4.3 cross-review, not touched by this fix), only
reachable via `test_concurrent_confirm_racing_manual_reported_to_faulty_
exactly_one_outcome`'s real-thread race and therefore not hit on every
single run by construction -- not a regression from this change. All five
race tests re-run again on the final code: the real-thread race test 200x
(0 failures), both deterministic tests and both
`test_device_registration.py` siblings 50x each (0 failures).

**Cross-review round 2: three follow-ups, all fixed here.**

1. **The `fleet/storage.py:2271` coverage wobble above is now
   deterministic, not scheduling luck.** New test
   `test_confirm_device_device_state_guard_fails_if_state_changes_mid_call`
   (`tests/test_device_lifecycle_registration_integration.py`, same
   technique as `test_confirm_device_registration_claim_fails_if_
   invalidated_mid_call` right below it, one guard earlier): the device is
   moved out of `reported` *within the same transaction*, via
   `Storage._write_inventory_audit_log`, at the moment the new
   assignment's own `"assigned"` audit row is written -- strictly after
   phase 2's own initial recheck already passed, strictly before the
   device-guarded `UPDATE` this test targets ever runs. `confirm_device`
   then fails cleanly (`"anderweitig bearbeitet"`) and rolls back
   everything: device back to `reported`, no new assignment, no new audit
   row beyond `prepare_device`'s own pre-existing `"prepared"` one, the
   registration untouched. No `# pragma: no cover` needed -- this branch is
   now exercised on every run.
2. **A missing or non-integer `expected_assignment_id` used to fall
   straight through to FastAPI's raw `422`, unlike every other field this
   route validates.** `STALE_ASSIGNMENT_MESSAGE` moved from a private
   `Storage` class attribute to a shared, public constant
   (`fleet.device_lifecycle.STALE_ASSIGNMENT_MESSAGE`, the same "defined
   once, used by both the storage layer and the route's own re-render"
   pattern `validate_manual_device_transition`'s message already
   established). `fleet/ui_routes.py::replace_device_submit` now types
   `expected_assignment_id: str | None` (same reasoning as `fleet
   .ui_apartment.clamp_history_days`'s own docstring for `days`), parses it
   itself, and re-renders the same `400` with the same message on a
   missing or unparseable value -- checked in the same place as
   `reason`/`target_state`, after the CSRF check. Two new tests
   (`tests/test_ui_inventory.py
   ::test_replace_device_submit_missing_expected_assignment_id_rerenders_
   with_a_message`, `::test_replace_device_submit_non_integer_expected_
   assignment_id_rerenders_with_a_message`): both `400`, nothing changed,
   no audit row.
3. **Positive test for the `had_token` audit fix.** Two new
   `tests/test_storage.py` tests assert the `token_revoked` row's
   `before_json` directly: `{"token_hash": "set"}` for an apartment that
   actually had one (`_make_apartment_with_device`'s own
   `set_apartment_token`), `{"token_hash": null}` for one that never did
   (built by hand, the same way `tests/test_ui_inventory.py
   ::test_replace_device_submit_old_token_gets_403_on_a_real_heartbeat`
   already reaches into `DeviceRecord.state` for a state P4.2/P4.2b give no
   route to set yet).

Verification for this round (same fresh venv): `ruff check .`, `mypy .`,
`mypy protocol fleet agent tools` all clean; `python -m pytest -W
ignore::ResourceWarning` run 3x -- **741 passed** every time, `TOTAL`
missed-statement count **identical across all three runs** (2948
statements, 21 missed, 99%), every touched file
(`fleet/storage.py`, `fleet/ui_inventory.py`, `fleet/ui_routes.py`,
`fleet/device_lifecycle.py`) at 100% -- the coverage wobble from the
previous round is gone. Both real-thread race tests
(`test_confirm_device_replace_previous_races_a_concurrent_remove_device`,
`test_concurrent_confirm_racing_manual_reported_to_faulty_exactly_one_
outcome`) re-run 200x each, 0 failures.

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
