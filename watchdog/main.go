// Entry point of the watchdog. -check-mode reads the state file and
// (optionally) the health report file and prints both -- the contract test
// from section 18.3, see check_contract.sh. Otherwise: the scaffold
// functions from watch.go.
package main

import (
	"flag"
	"fmt"
	"os"
)

func main() {
	file := flag.String("file", "", "path to the state file")
	healthFile := flag.String("health-file", "", "path to the health report file (optional)")
	checkMode := flag.Bool("check-mode", false, "only read and print")
	flag.Parse()

	if *checkMode {
		os.Exit(runCheckMode(*file, *healthFile))
	}
	fmt.Fprintln(os.Stderr, "thermoctl-watchdog: only the scaffold is present. See docs/STATUS.md for the implementation status.")
	os.Exit(1)
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
