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
`watch.go` now implements the actual sequence from section 17, steps 3-6:

- `AgentStopped` detects that the agent has stopped itself, via `Runtime`
  (below), never by watching the container runtime directly.
- `StartDigest` starts the revision named in `desired`.
- `AwaitHealthReport` waits, at most 10 minutes (`maxHealthPolls`, a poll
  count rather than a wall-clock deadline -- see its doc comment), for a
  health report naming that same digest with a timestamp at or after
  `since` (section 22.2/22.3) -- an older, or differently digested, report
  does not count. It also gives up early after three restarts in a row.
- `RollBackToProven` starts `proven` and notes the reason on **stderr**,
  captured by journald under the systemd unit -- deliberately not a new
  file format for a single line nobody reads back programmatically. An
  empty `proven` is reported as an error, not guessed at (section 22.5,
  "Fallback without a proven revision").
- `Reconcile` ties these four together into one pass, and returns an
  `Outcome` -- the single hook P5.7 needs to drive `leds.go` without
  touching this decision again. `desired == proven` skips the
  await/rollback dance entirely: there is no different digest to fall back
  to, so the agent is simply restarted.
- `main.go`'s `runLoop` calls `Reconcile` on an interval, forever --
  `systemd` (`Restart=always`) is the backstop if the process itself dies,
  not a reason for the loop to give up on its own.

**`Runtime` (`runtime.go`) is the seam onto the container runtime (section
18.3): addressed only through `os/exec`, never a library.** Tests substitute
a fake implementation and need no Docker at all. The production
implementation, `cliRuntime`, is a plain `docker` wrapper (`-runtime-bin`/
`-runtime-container` flags override the binary and the container name); a
runtime that speaks an entirely different command set gets its own
`Runtime` implementation, not more flags.

**No wall clock in tests.** `AwaitHealthReport` and `Reconcile` take a
`sleep func(time.Duration)` instead of calling `time.Sleep` -- production
passes `time.Sleep` itself, tests pass a no-op that just counts calls, so
the 10-minute deadline never costs a test run real minutes.

`check_contract.sh` and the state/health file format are unchanged by this
task -- no new file, no new line in either format.
