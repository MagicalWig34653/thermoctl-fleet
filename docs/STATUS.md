# Status

Last updated: 2026-09-24.

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
  `watchdog/`, `agent/loop.py`, or `protocol/`.
- **`image.yml`** (new): reads the `image/` configuration and validates the
  package list (`tools/check_image_config.py`) -- **no** real pi-gen/mkosi/
  debos run, that belongs at the release, not in every commit.
- **`docker.yml`** builds two images (`thermoctl-fleet`, `thermoctl-agent`)
  for `linux/amd64` and `linux/arm64` to ghcr.io on `v*` tags, and on every
  pull request as a build check without publishing. `watchdog/` goes into
  **neither** of the two images (no Docker image, see above).
