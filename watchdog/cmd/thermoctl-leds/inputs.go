// The four local, line-based files this program reads (never a network,
// never a registry -- see README.md). Three formats already exist
// elsewhere (the watchdog's state file and health report, section 17/
// 18.3/22.3; P5.0's registration status file, agent/registration.py's
// `_write_status`); the fourth (agent status) is new, defined here and in
// docs/STATUS.md's P5.7 entry.
//
// This file deliberately re-implements the tiny "key=value per line, '#'
// comments" parser rather than importing watchdog's own linefile.go: that
// file lives in `package main` at the watchdog's module root, and a `main`
// package cannot be imported by another program in the same module. The
// duplication is a package-boundary consequence, not a shortcut -- kept to
// the same handful of lines the original already was.
package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

// readKeyValueLines reads r line by line as "key=value" into a map. Empty
// lines and comments ("#") are skipped; a line without "=" is an error.
func readKeyValueLines(r io.Reader, file string) (map[string]string, error) {
	values := map[string]string{}
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, found := strings.Cut(line, "=")
		if !found {
			return nil, fmt.Errorf("%s: line without '=': %q", file, line)
		}
		values[strings.TrimSpace(key)] = strings.TrimSpace(value)
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return values, nil
}

func parseTimestamp(value, fieldName, file string) (int64, error) {
	if value == "" {
		return 0, fmt.Errorf("%s: %q is missing or empty", file, fieldName)
	}
	timestamp, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%s: %q is not a Unix timestamp: %w", file, fieldName, err)
	}
	return timestamp, nil
}

// WatchdogState is the part of the watchdog's own state file (section 17
// step 2, `watchdog/state.go`) this program needs: which digest is
// currently desired, and since when.
type WatchdogState struct {
	Desired string
	Since   int64
}

func parseWatchdogState(r io.Reader) (WatchdogState, error) {
	values, err := readKeyValueLines(r, "state file")
	if err != nil {
		return WatchdogState{}, err
	}
	since, err := parseTimestamp(values["since"], "since", "state file")
	if err != nil {
		return WatchdogState{}, err
	}
	return WatchdogState{Desired: values["desired"], Since: since}, nil
}

// HealthReport mirrors watchdog/health.go's own format (section 22.3):
// `timestamp=`, `digest=`, `version=` -- only the first two matter here.
type HealthReport struct {
	Timestamp int64
	Digest    string
}

func parseHealthReport(r io.Reader) (HealthReport, error) {
	values, err := readKeyValueLines(r, "health report")
	if err != nil {
		return HealthReport{}, err
	}
	timestamp, err := parseTimestamp(values["timestamp"], "timestamp", "health report")
	if err != nil {
		return HealthReport{}, err
	}
	return HealthReport{Timestamp: timestamp, Digest: values["digest"]}, nil
}

// RegistrationStatus mirrors agent/registration.py's `_write_status`
// format (P5.0): `status=` (`waiting_for_assignment` or `assigned`),
// optional `verification_code=` (unused here, section 23.2 only needs
// `status`). No timestamp -- this file is written on real state
// transitions, not periodically, so no staleness window applies to it
// (see decide.go's own comment).
type RegistrationStatus struct {
	Status string
}

func parseRegistrationStatus(r io.Reader) (RegistrationStatus, error) {
	values, err := readKeyValueLines(r, "registration status file")
	if err != nil {
		return RegistrationStatus{}, err
	}
	return RegistrationStatus{Status: values["status"]}, nil
}

// AgentStatus is the new file this task adds (docs/STATUS.md's P5.7
// entry has the authoritative format description; agent/loop.py
// ::report_led_status is the Python writer, tests/test_watchdog_contract
// .py and watchdog/check_contract.sh cover the cross-language contract).
// Line-based like the other three, for the same "readable with built-in
// tools in any language" reasoning: `timestamp=`, `cloud_contact=` (`ok`/
// `lost`), `fault=` (`none`/`open`), `control=` (`ok`/`stalled`).
type AgentStatus struct {
	Timestamp    int64
	CloudContact string
	Fault        string
	Control      string
}

func parseAgentStatus(r io.Reader) (AgentStatus, error) {
	values, err := readKeyValueLines(r, "agent status file")
	if err != nil {
		return AgentStatus{}, err
	}
	timestamp, err := parseTimestamp(values["timestamp"], "timestamp", "agent status file")
	if err != nil {
		return AgentStatus{}, err
	}
	return AgentStatus{
		Timestamp:    timestamp,
		CloudContact: values["cloud_contact"],
		Fault:        values["fault"],
		Control:      values["control"],
	}, nil
}

// loadOptional opens path and hands it to parse, returning (nil, nil) if
// the file does not exist at all -- a missing file is an entirely
// ordinary condition here (not yet registered, agent not yet started,
// watchdog freshly installed before its first state write), never logged
// as an error on every poll (section 23.3's "no error spam", applied by
// analogy to input files, not just to a missing LED driver). The same
// "open, defer close, parse" shape as watchdog/linefile.go::openAndParse,
// factored out here for the identical reason: four call sites
// (loadWatchdogState, loadHealthReport, loadRegistrationStatus,
// loadAgentStatus below) would otherwise repeat it four times over.
func loadOptional[T any](path string, parse func(io.Reader) (T, error)) (*T, error) {
	if path == "" {
		return nil, nil
	}
	file, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer file.Close()
	value, err := parse(file)
	if err != nil {
		return nil, err
	}
	return &value, nil
}

func loadWatchdogState(path string) (*WatchdogState, error) {
	return loadOptional(path, parseWatchdogState)
}

func loadHealthReport(path string) (*HealthReport, error) {
	return loadOptional(path, parseHealthReport)
}

func loadRegistrationStatus(path string) (*RegistrationStatus, error) {
	return loadOptional(path, parseRegistrationStatus)
}

func loadAgentStatus(path string) (*AgentStatus, error) {
	return loadOptional(path, parseAgentStatus)
}
