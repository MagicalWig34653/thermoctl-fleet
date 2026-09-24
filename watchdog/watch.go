// Scaffold of the actual watchdog decisions (section 17). Unlike state.go
// and health.go (pure, real file parsing), every function here needs real
// access to the container runtime or systemd, which this scaffold does not
// anticipate -- each one therefore returns an error referencing the
// responsible step instead of pretending something happened. Division of
// labor (section 17, "who loads, and who swaps"): no function here calls a
// registry or checks a digest -- the agent has already done that. The
// watchdog only knows two locally present digests.
package main

import "fmt"

// AgentStopped detects that the agent has stopped itself (step 3). The
// agent does not swap itself -- it only stops. The watchdog must notice the
// end before it does anything.
func AgentStopped() (bool, error) {
	return false, fmt.Errorf("detecting the agent's end not implemented -- see docs/specification.md section 17")
}

// StartDigest starts the revision named in s.Desired (step 4). No digest
// check against a source list here -- see the module docstring.
func StartDigest(s State) error {
	return fmt.Errorf("starting digest %q not implemented -- see docs/specification.md section 17", s.Desired)
}

// AwaitHealthReport waits for the health report, at most 10 minutes
// (step 5), or detects a container restarting three times.
func AwaitHealthReport() (bool, error) {
	return false, fmt.Errorf("waiting for the health report not implemented -- see docs/specification.md section 17")
}

// RollBackToProven falls back to s.Proven when the health report fails to
// arrive or after three restarts (step 5). An empty s.Proven has not been
// the normal state since section 22.5: the image recipe pre-sets it at
// build time, so every shipped device has a fallback target.
func RollBackToProven(s State) error {
	if s.Proven == "" {
		return fmt.Errorf("no proven digest present -- sign of a faulty delivery, see docs/specification.md section 22.5")
	}
	return fmt.Errorf("rolling back to %q not implemented -- see docs/specification.md section 17", s.Proven)
}
