package main

import (
	"fmt"
	"os"
	"time"

	"github.com/magicalwig34653/thermoctl-fleet/watchdog/internal/ledsysfs"
)

func ledPresent(path string) bool {
	return ledsysfs.LedPresent(path)
}

// gatherInputs reads all four input files, tolerating any that are
// missing (see inputs.go's loadXxx functions) -- that is the ordinary
// state for at least one of them on almost every real device (e.g. a
// mobile-data-only apartment never gets a registration status file past
// "assigned"; a brand-new device has no agent status file before its
// first loop tick). A read error that is *not* "file does not exist"
// (e.g. an unreadable, half-written, or malformed file) is reported on
// stderr once per occurrence -- not silently swallowed, since that would
// hide a real bug in whichever writer produced it -- but does not stop
// the pass: the corresponding input is simply treated as absent, and the
// staleness/precedence rules already handle "absent" conservatively.
func gatherInputs(stateFile, healthFile, registrationFile, agentStatusFile string, staleAfter time.Duration, now func() time.Time) Inputs {
	state, err := loadWatchdogState(stateFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "thermoctl-leds: reading state file: %v\n", err)
	}
	health, err := loadHealthReport(healthFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "thermoctl-leds: reading health report: %v\n", err)
	}
	registration, err := loadRegistrationStatus(registrationFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "thermoctl-leds: reading registration status file: %v\n", err)
	}
	agentStatus, err := loadAgentStatus(agentStatusFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "thermoctl-leds: reading agent status file: %v\n", err)
	}
	return Inputs{
		Now:          now(),
		StaleAfter:   staleAfter,
		State:        state,
		Health:       health,
		Registration: registration,
		AgentStatus:  agentStatus,
	}
}

// applyOnce gathers the current inputs, decides both patterns, and writes
// them -- the single pass runLoop repeats forever. Split out for its own
// direct test, the same shape as watchdog/watch.go::Reconcile/main.go
// ::runLoop.
func applyOnce(stateFile, healthFile, registrationFile, agentStatusFile, ledDevice, ledSystem string, staleAfter time.Duration, now func() time.Time) {
	in := gatherInputs(stateFile, healthFile, registrationFile, agentStatusFile, staleAfter, now)
	if err := ledsysfs.ApplyPattern(ledDevice, DecideDevice(in)); err != nil {
		fmt.Fprintf(os.Stderr, "thermoctl-leds: driving LED 1 (device): %v\n", err)
	}
	if err := ledsysfs.ApplyPattern(ledSystem, DecideSystem(in)); err != nil {
		fmt.Fprintf(os.Stderr, "thermoctl-leds: driving LED 2 (system): %v\n", err)
	}
}

// runLoop calls applyOnce on an interval, forever -- systemd
// (Restart=always, see thermoctl-leds.service) is the backstop if this
// process itself dies, the same reasoning as the watchdog's own runLoop.
func runLoop(stateFile, healthFile, registrationFile, agentStatusFile, ledDevice, ledSystem string, staleAfter time.Duration, now func() time.Time, sleep func(time.Duration)) {
	for {
		applyOnce(stateFile, healthFile, registrationFile, agentStatusFile, ledDevice, ledSystem, staleAfter, now)
		sleep(pollInterval)
	}
}

// runCheckMode prints the agent status file's parsed values in uppercase
// keys, matching watchdog/main.go's own -check-mode convention -- the
// contract test (watchdog/check_contract.sh) builds on this exactly the
// way it already does for the watchdog's state/health files.
func runCheckMode(agentStatusFile string) int {
	status, err := loadAgentStatus(agentStatusFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "thermoctl-leds: %v\n", err)
		return 2
	}
	if status == nil {
		fmt.Fprintln(os.Stderr, "thermoctl-leds: agent status file is missing")
		return 2
	}
	fmt.Printf("AGENT_STATUS_TIMESTAMP=%d\nCLOUD_CONTACT=%s\nFAULT=%s\nCONTROL=%s\n",
		status.Timestamp, status.CloudContact, status.Fault, status.Control)
	return 0
}
