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
- **Under 300 lines.** If it grows beyond that, the scope is wrong.
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

## State of this scaffold

`state.go` and `health.go` are actually implemented (pure parsing, no
third-party packages). `watch.go` (detecting the agent's end, starting a
digest, waiting for the health report, falling back to `proven`) is a
scaffold -- every function returns an error referencing section 17, none is
implemented.
