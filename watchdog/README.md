# watchdog -- the process that swaps the agent (section 17, 18.3)

**Written in Go, not in Python.** The watchdog is the one thing that has to
work when everything else is broken. A Python program assumes the
interpreter is intact: no half-applied `apt` transaction, no shot `python3`
symlink after a version jump, no corrupted `.pyc` file on a dying card. A
statically linked binary does not know this class of failure. Go instead of
Rust, because the standard library decides it: time handling and file work
are included, cross-compiling for `arm64`/`amd64` needs no extra tooling,
and Rust's strength -- safety when processing untrusted data -- pays off
little here, because the watchdog only reads a file written by its own
sibling process and calls `systemctl`/the container runtime. No network, no
untrusted input.

**No Docker image.** It runs outside the container runtime -- it starts and
stops containers and must be present precisely when they are not running.
It is shipped with the prepared system image (`image/`) and a systemd unit
(`thermoctl-watchdog.service`), updated via the operating system's package
management -- deliberately outside the fleet service.

## Conditions (section 18.3, adopted verbatim)

- **`go.mod` without a single dependency.** In particular not the Docker
  SDK -- the container runtime is addressed via its command-line tool or
  via `systemctl`.
- **Statically built** (`CGO_ENABLED=0`), one binary each for `arm64` and
  `amd64`, with a checksum, produced in CI and placed into the image.
  Nothing is compiled on the device.
- **Under 300 lines of executable statements** (comments and blank lines do
  not count, section 18.3's redefined rule). If it grows beyond that, the
  scope is wrong.
- Own CI track (`.github/workflows/go.yml`): `go vet`, `go test`, build for
  both architectures. The Python track (`ci.yml`) stays unchanged.

## Who loads, and who swaps (section 17)

The **agent** (Python, `agent/`) downloads a new image, checks its digest
against the hard-coded sources, and then writes both digests into the state
file (`state.go`). The **watchdog** never sees a network, knows no registry,
and checks no signatures -- it knows two locally present digests and one
question: *has the agent said "I'm healthy" within the deadline?* This
separation is the reason the capabilities the watchdog **does not** have
are just as important as the ones it has: a watchdog with registry access
would be a second path by which foreign code reaches the device.

## The contract with the agent

Two files, both line-based (`KEY=VALUE`, like a systemd environment file),
**no JSON** -- the reasoning for that is a comment in `state.go`, at the
point where it matters, so it is not lost if someone later wants to
"simplify" the format:

- `state.go`: reads the state file written by Python
  (`agent.loop.report_watchdog_state`) (`desired`, `proven`, `since`, plus the
  two eSIM rollback lines from section 24.4).
- `health.go`: reads the health report periodically written by the running
  agent -- three lines (`timestamp=`, `digest=`, `version=`), not a bare
  timestamp. The digest is the point: it lets the watchdog tell that *the right*
  version is alive, not merely that something is (section 22.3).

`check_contract.sh` is the cross-language contract test from section 18.3:
Python writes, the built Go binary reads in check mode (`-check-mode`), the
result is compared. It runs in `.github/workflows/go.yml` -- see that
file's comment for the reasoning why it lives there and not in `pytest` or
`go test` alone.

## The main loop (`watch.go`, `main.go`, `runtime.go`) -- P5.6

`state.go` and `health.go` are pure parsing, no third-party packages.
`watch.go` implements the actual sequence from section 17, steps 3-6:

- `AwaitHealthReport` waits until `since + 10 minutes` (section 22.2 --
  anchored to the state file's own `since`, not to whenever the call
  started, so a watchdog restart mid-window resumes with only the time
  actually left, never a fresh 10 minutes) for a health report naming the
  desired digest with a timestamp at or after `since` (section 22.3) -- an
  older, or differently digested, report does not count. It also gives up
  early after three restarts in a row.
- `RollBackToProven` starts `proven` and notes the reason on **stderr**,
  captured by journald under the systemd unit -- deliberately not a new
  file format for a single line nobody reads back programmatically. An
  empty `proven` is reported as an error, not guessed at (section 22.5,
  "Fallback without a proven revision").
- `Reconcile` ties detecting the agent's end (step 3), starting what is
  needed (step 4), and the two functions above (step 5) into one pass, and
  returns an `Outcome`. **P5.7 (section 23) ended up not using this return
  value**: the project owner decided afterward that the status LEDs are
  driven by a separate small program, `cmd/thermoctl-leds/`, reading its
  own local files (state file, health report, the P5.0 registration status
  file, and a new agent-written status file) directly, rather than
  observing this process's in-memory `Outcome` -- see that program's own
  README section for the reasoning and `docs/STATUS.md`'s P5.7 entry.
  `Outcome` stays as documentation of what one pass decided; nothing here
  reads it back. `desired == proven` skips the
  await/rollback dance entirely: there is no different digest to fall back
  to, so the agent is simply started when stopped. A container already
  running the desired digest, but not yet proven, re-enters the
  await/rollback path instead of being mistaken for "done" -- the
  resumability fix from P5.6's own cross-review. (`AgentStopped` and
  `StartDigest` from the original scaffold are gone as separate functions:
  `Reconcile` needs the running digest from the same `Status` call
  `AgentStopped` would have made on its own, and `StartDigest` had exactly
  one call site once `Reconcile` existed -- both inlined, see
  `docs/STATUS.md` for the line-budget reasoning.)
- `main.go`'s `runLoop` calls `Reconcile` on an interval, forever --
  `systemd` (`Restart=always`) is the backstop if the process itself dies,
  not a reason for the loop to give up on its own.

**`Runtime` (`runtime.go`) is the seam onto the container runtime (section
18.3): addressed only through `os/exec`, never a library.** Tests substitute
a fake implementation (or, for `cliRuntime` itself, swap the package-level
`execCommand` variable for a recording fake) and need no Docker at all.

**`Start` never runs a digest directly.** A bare `docker run -d --name
thermoctl-agent <digest>` has no restart policy (so Docker never counts a
restart, and "three restarts in a row" could never trigger) and no volumes
for the agent to actually do its job. The agent's real run configuration
instead lives in a fixed compose file shipped with the system image
(`image/common/agent-compose.yml`, never sent by the cloud -- section 13's
"no arbitrary compose files" is about what the cloud may hand the agent,
not about this one fixed file). `Start` only (a) locally tags the digest as
`<repo>@<digest>` (never a bare digest -- see below) under the fixed name
that file references, no network, and (b) re-applies that file with
`--pull never` (belt and suspenders with the file's own `pull_policy:
never`) so a missing image is refused, not fetched from a registry.
`digestPattern` (`^sha256:[0-9a-f]{64}$`) refuses anything else before it
ever reaches argv -- a value starting with `-` must never be parsed as a
command-line option.

**`desired`/`proven` are registry *manifest* digests, not local image
IDs.** `docker inspect -f '{{.Image}}'` reports the container's *local*
image ID -- a different hash for the same image. `Status` resolves the
running container's manifest digest via its image's `RepoDigests`
(`cliRuntime.repoDigest`), matched against `-runtime-repo` (default
`ghcr.io/magicalwig34653/thermoctl-agent`, the agent's own hard-coded
source, security principle 2 -- never the cloud's to name). No matching
entry reports an empty digest, which `Reconcile` already treats as
"something other than desired is running". **Contract note for P5.4**
(agent-side desired-state reconciliation, not yet built): the agent must
pull the agent image by digest (`<repo>@sha256:...`), or the running
container will carry no matching `RepoDigests` entry and the watchdog can
never confirm it is healthy.

**No wall clock in tests.** `AwaitHealthReport` and `Reconcile` take
`now func() time.Time` and `sleep func(time.Duration)` instead of calling
`time.Now`/`time.Sleep` directly -- production passes the real functions,
tests pass a fake clock that advances only when `sleep` is called, so the
10-minute deadline never costs a test run real minutes, even while
resuming mid-window.

`check_contract.sh` and the state/health file format are unchanged by this
task -- no new file, no new line in either format.

## `cmd/thermoctl-leds/` -- the status LEDs (section 23, P5.7)

**Not the watchdog itself.** The implementation plan originally had P5.7
wire section 23's two LEDs directly into `watch.go`'s `Reconcile`, but the
project owner decided afterward (2026-09-26, see
`docs/specification.md` section 23's own "Decided afterward" paragraph)
that they are driven by a **separate small program in this same module**
instead: `cmd/thermoctl-leds/`, its own `go build ./cmd/thermoctl-leds`,
its own systemd unit (`thermoctl-leds.service`, next to the binary's code,
same placement convention as `thermoctl-watchdog.service`). Two reasons:
the watchdog's own 300-line budget had only one line of headroom left
after P5.6 (`docs/STATUS.md`), and -- the more important one -- a bug in
LED-driving code must never be able to reach the process whose only job is
swapping and rolling back the agent reliably. Building `.` in `watchdog/`
still produces only the watchdog binary, unchanged; `cmd/thermoctl-leds`
is a second, independent `main` package the same `go.mod` (no
dependencies either) also covers.

**Shared code:** `internal/ledsysfs` (`LedPresent`, `ApplyPattern`) is what
used to be `watchdog/leds.go`/`leds_test.go`, moved -- not duplicated --
into an internal package once the watchdog itself stopped needing it. The
watchdog's own line count therefore **went down**, not merely "did not
grow": removing `leds.go` (19 statement lines) and its test took the seven
production files from **299** to **280** statement lines before
`cmd/thermoctl-leds` added anything of its own (that program's files are
no longer part of the watchdog's own budget at all, being a separate `go
build` target) -- see `docs/STATUS.md`'s P5.7 entry for the exact
before/after count of both programs.

**Inputs, all local files, never a network:**

- the watchdog's own state file and health report (unchanged formats,
  `state.go`/`health.go` above) -- re-parsed by `cmd/thermoctl-leds/inputs
  .go`'s own small copy of the key-value-line reader, not by importing
  `state.go`/`health.go` directly: those live in `package main` at this
  module's root, and a `main` package cannot be imported by another
  program in the same module. Duplication forced by that package
  boundary, not a shortcut -- kept to the same handful of lines the
  original already was.
- `agent/registration.py`'s `registration_status` file (P5.0): `status=`
  drives the "waiting for assignment" fast blink.
- a **new** agent-written status file for what only the agent can know --
  format, staleness window, and precedence rules documented in
  `cmd/thermoctl-leds/decide.go` and `docs/STATUS.md`'s P5.7 entry, and
  written on the Python side by `agent.loop.report_led_status` (covered by
  `tests/test_watchdog_contract.py` and this package's own
  `check_contract.sh` extension).

**Missing LED driver (section 23.3, "Raspberry Pi only"):** checked once
at startup (`ledPresentEither`), same as the watchdog's own `LedPresent`
used to be checked per call -- if neither of the two sysfs files exists at
all, the program logs one line and exits cleanly (exit 0) instead of
looping forever for nothing; `thermoctl-leds.service` uses
`Restart=on-failure`, not `Restart=always`, so that clean exit is not
treated as a crash to restart.
