// Entry point of the watchdog. -check-mode reads the state file and
// (optionally) the health report file and prints both -- the contract test
// from section 18.3, see check_contract.sh. Otherwise: the main loop from
// watch.go, reconciling on the state and health files at a fixed interval.
package main

import (
	"flag"
	"fmt"
	"os"
	"time"
)

// loopInterval is how often the main loop reconciles (below "runLoop").
const loopInterval = 5 * time.Second

func main() {
	file := flag.String("file", "", "path to the state file")
	healthFile := flag.String("health-file", "", "path to the health report file")
	checkMode := flag.Bool("check-mode", false, "only read and print")
	// Sane defaults, per section 18.3: no hard-coded path beyond the plain
	// binary name -- an apartment with a differently named container, or a
	// non-default docker binary, overrides both via flags.
	bin := flag.String("runtime-bin", "docker", "the container runtime's command-line tool")
	container := flag.String("runtime-container", "thermoctl-agent", "name of the agent container")
	flag.Parse()

	if *checkMode {
		os.Exit(runCheckMode(*file, *healthFile))
	}
	runLoop(cliRuntime{bin: *bin, container: *container}, *file, *healthFile, time.Sleep)
}

// runLoop reconciles once, sleeps, and repeats -- forever. systemd
// (Restart=always, see thermoctl-watchdog.service) is the backstop if this
// process itself dies; that is not a reason for the loop to give up on its
// own (section 17).
func runLoop(rt Runtime, stateFile, healthFile string, sleep func(time.Duration)) {
	for {
		state, err := LoadState(stateFile)
		if err != nil {
			fmt.Fprintf(os.Stderr, "thermoctl-watchdog: reading state file: %v\n", err)
			sleep(loopInterval)
			continue
		}
		if _, err := Reconcile(rt, state, healthFile, sleep); err != nil {
			fmt.Fprintf(os.Stderr, "thermoctl-watchdog: %v\n", err)
		}
		sleep(loopInterval)
	}
}

// runCheckMode prints both files in uppercase keys (comparison script).
func runCheckMode(file, healthFile string) int {
	if file == "" {
		fmt.Fprintln(os.Stderr, "thermoctl-watchdog: -file is missing")
		return 2
	}
	state, err := LoadState(file)
	if err != nil {
		return reportError(err)
	}
	fmt.Printf("DESIRED=%s\nPROVEN=%s\nSINCE=%d\nESIM_PREVIOUS_PROFILE=%s\nESIM_DEADLINE=%d\n",
		state.Desired, state.Proven, state.Since, state.EsimPreviousProfile, state.EsimDeadline)
	if healthFile == "" {
		return 0
	}
	health, err := ReadHealth(healthFile)
	if err != nil {
		return reportError(err)
	}
	fmt.Printf("HEALTH_TIMESTAMP=%d\nHEALTH_DIGEST=%s\nHEALTH_VERSION=%s\n",
		health.Timestamp, health.Digest, health.Version)
	return 0
}

func reportError(err error) int {
	fmt.Fprintf(os.Stderr, "thermoctl-watchdog: %v\n", err)
	return 2
}
