// thermoctl-leds -- the status-LED display (section 23), a separate small
// program next to the watchdog (docs/specification.md section 23,
// "Decided afterward", 2026-09-26). See watchdog/README.md's own note on
// why this program exists instead of the watchdog driving the LEDs
// itself, and this package's own comments (inputs.go, decide.go) for the
// file contract and the precedence/staleness rules.
//
// No dependency in go.mod (shared with the watchdog, same module), no
// network, no registry: every input is a local file, every output a
// sysfs write via internal/ledsysfs.
package main

import (
	"flag"
	"fmt"
	"os"
	"time"
)

// pollInterval mirrors the watchdog's own loop cadence (main.go's
// loopInterval) -- there is no reason for the display to notice a state
// change any slower than the watchdog itself does.
const pollInterval = 5 * time.Second

// defaultStaleAfter is 3x the heartbeat interval (120s, section 5) --
// documented here since docs/STATUS.md's P5.7 entry is the canonical
// place, not repeated at every call site. Long enough that one merely
// slow or delayed report does not flip the display to "unknown"; short
// enough that a genuinely stopped agent is shown within a few minutes,
// not indefinitely as "still fine".
const defaultStaleAfter = 3 * 120 * time.Second

func main() {
	// -health-file and -agent-status-file both default under
	// /run/thermoctl-agent/ (P5.7 hot-fix, docs/STATUS.md): that whole
	// directory, not either file individually, is what
	// image/common/agent-compose.yml bind-mounts into the agent container
	// -- a single-file bind mount cannot be replaced by the temp-file-
	// plus-rename pattern agent/loop.py writes both files with.
	stateFile := flag.String("state-file", "/var/lib/thermoctl-watchdog/state.env", "the watchdog's own state file (section 17 step 2)")
	healthFile := flag.String("health-file", "/run/thermoctl-agent/health.env", "the watchdog's own health report (section 22.3)")
	registrationFile := flag.String("registration-status-file", "/var/lib/thermoctl-agent/registration_status", "P5.0's registration status file")
	agentStatusFile := flag.String("agent-status-file", "/run/thermoctl-agent/led-status.env", "the new agent-written status file this task adds (docs/STATUS.md P5.7)")
	ledDevice := flag.String("led-device", "/sys/class/leds/thermoctl-device/brightness", "LED 1 (device, green)")
	ledSystem := flag.String("led-system", "/sys/class/leds/thermoctl-system/brightness", "LED 2 (system, yellow)")
	staleAfter := flag.Duration("stale-after", defaultStaleAfter, "how old a periodic report may be before it is treated as unknown")
	checkMode := flag.Bool("check-mode", false, "only read and print the agent status file (contract test, see watchdog/check_contract.sh)")
	flag.Parse()

	if *checkMode {
		os.Exit(runCheckMode(*agentStatusFile))
	}

	// Section 23.3: Raspberry Pi only. A mini PC has neither file -- that
	// is not an error, and this program must not sit in a pointless
	// forever-loop nor spam a log line every poll interval about
	// something that will never change: check once, at startup, and
	// exit cleanly if there is nothing to drive at all.
	if !ledPresentEither(*ledDevice, *ledSystem) {
		fmt.Fprintln(os.Stderr, "thermoctl-leds: no LED driver present (not a Raspberry Pi, or the gpio-led overlay is not loaded) -- exiting, see docs/specification.md section 23.3")
		return
	}

	runLoop(*stateFile, *healthFile, *registrationFile, *agentStatusFile, *ledDevice, *ledSystem, *staleAfter, time.Now, time.Sleep)
}

func ledPresentEither(ledDevice, ledSystem string) bool {
	return ledPresent(ledDevice) || ledPresent(ledSystem)
}
