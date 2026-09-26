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
	// binary name -- a non-default docker binary, or a compose file at
	// another path, overrides via flags. The container name itself is not a
	// flag: it is fixed by the compose file's own container_name (see
	// runtime.go's agentContainerName). -runtime-repo must match the
	// agent's own hard-coded source list (security principle 2) -- it is
	// never taken from the cloud, only confirmed once by whoever prepares
	// the image.
	bin := flag.String("runtime-bin", "docker", "the container runtime's command-line tool")
	compose := flag.String("runtime-compose", "/etc/thermoctl-agent/compose.yml", "fixed compose file the image ships (never from the cloud)")
	repo := flag.String("runtime-repo", "ghcr.io/magicalwig34653/thermoctl-agent", "the agent's hard-coded image source -- must match agent/'s own source list")
	flag.Parse()

	if *checkMode {
		os.Exit(runCheckMode(*file, *healthFile))
	}
	runLoop(cliRuntime{bin: *bin, compose: *compose, repo: *repo}, *file, *healthFile, time.Now, time.Sleep)
}

// runLoop reconciles once, sleeps, and repeats -- forever. systemd
// (Restart=always, see thermoctl-watchdog.service) is the backstop if this
// process itself dies; that is not a reason for the loop to give up on its
// own (section 17).
func runLoop(rt Runtime, stateFile, healthFile string, now func() time.Time, sleep func(time.Duration)) {
	for {
		if state, err := LoadState(stateFile); err != nil {
			fmt.Fprintf(os.Stderr, "thermoctl-watchdog: reading state file: %v\n", err)
		} else if _, err := Reconcile(rt, state, healthFile, now, sleep); err != nil {
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
		fmt.Fprintf(os.Stderr, "thermoctl-watchdog: %v\n", err)
		return 2
	}
	fmt.Printf("DESIRED=%s\nPROVEN=%s\nSINCE=%d\nESIM_PREVIOUS_PROFILE=%s\nESIM_DEADLINE=%d\n",
		state.Desired, state.Proven, state.Since, state.EsimPreviousProfile, state.EsimDeadline)
	if healthFile == "" {
		return 0
	}
	health, err := ReadHealth(healthFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "thermoctl-watchdog: %v\n", err)
		return 2
	}
	fmt.Printf("HEALTH_TIMESTAMP=%d\nHEALTH_DIGEST=%s\nHEALTH_VERSION=%s\n",
		health.Timestamp, health.Digest, health.Version)
	return 0
}
