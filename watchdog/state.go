// The contract between agent and watchdog: the state file (section 17,
// step 2; section 18.3). Line-based, NOT JSON -- intentional, not a
// simplification: this way the contract is readable in every language with
// built-in tools. A freshly shipped device never starts with an empty
// Proven: the image recipe enters the digest of the shipped version as both
// Desired and Proven at build time (section 22.5) -- an empty Proven is
// thus a sign of a faulty delivery, not the normal state.
package main

import (
	"fmt"
	"io"
)

// State is the parsed content of the state file. Desired: the digest already
// checked by the agent (section 13). Proven: digest after one fault-free
// hour, pre-set at build time (section 22.5). Since: since when Desired has
// applied (section 22.2, decided afterward -- the only reading with which
// this field can compute the deadlines from section 17). EsimPreviousProfile
// /EsimDeadline: fallback clock for an eSIM profile switch (section 24.4,
// decided afterward as two further lines here, no separate file). Empty/0
// as long as no switch is pending.
type State struct {
	Desired             string
	Proven              string
	Since               int64
	EsimPreviousProfile string
	EsimDeadline        int64
}

// ParseState reads a state file from r. Unknown keys are ignored, not
// rejected -- a future line unknown to the watchdog must not keep it from
// reading the known ones (section 18.2, applied analogously).
func ParseState(r io.Reader) (State, error) {
	values, err := readKeyValueLines(r, "state file")
	if err != nil {
		return State{}, err
	}
	if values["desired"] == "" {
		return State{}, fmt.Errorf("state file: 'desired' is missing or empty")
	}
	since, err := parseOptionalTimestamp(values["since"], "since", "state file")
	if err != nil {
		return State{}, err
	}
	esimDeadline, err := parseOptionalTimestamp(values["esim_deadline"], "esim_deadline", "state file")
	if err != nil {
		return State{}, err
	}
	return State{
		Desired:             values["desired"],
		Proven:              values["proven"],
		Since:               since,
		EsimPreviousProfile: values["esim_previous_profile"],
		EsimDeadline:        esimDeadline,
	}, nil
}

// LoadState opens path and parses it. No network, no registry.
func LoadState(path string) (State, error) {
	return openAndParse(path, ParseState)
}
