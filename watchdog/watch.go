// The actual watchdog decisions (section 17). Unlike state.go and health.go
// (pure, real file parsing), everything here needs the container runtime or
// systemd -- addressed exclusively through the tiny Runtime interface below,
// itself backed by os/exec in runtime.go, never a library (section 18.3).
// Division of labor (section 17, "who loads, and who swaps"): no function
// here calls a registry or checks a digest -- the agent has already done
// that. The watchdog only knows two locally present digests.
package main

import (
	"fmt"
	"os"
	"time"
)

// healthPollInterval is how often AwaitHealthReport re-checks the health
// report and the restart count while waiting out the deadline below.
const healthPollInterval = 5 * time.Second

// healthDeadline is the 10 minutes from section 17, step 5, within which a
// newly started revision must prove itself, expressed as a poll count so
// AwaitHealthReport needs no notion of wall-clock time at all -- only a
// sleep function, which tests replace with one that does not actually
// wait.
const maxHealthPolls = int(10 * time.Minute / healthPollInterval)

// maxRestarts: three restarts in a row is a failed swap (section 17, step
// 5), independent of the deadline above.
const maxRestarts = 3

// Runtime is the seam onto the container runtime (section 18.3): addressed
// only through this interface, never a library -- tests substitute a fake,
// never Docker.
type Runtime interface {
	// Start starts the revision identified by digest.
	Start(digest string) error
	// Status reports whether the agent is currently running, the digest it
	// was started from if so, and how many times it has restarted in a row
	// since that start.
	Status() (running bool, digest string, restarts int, err error)
}

// AgentStopped detects that the agent has stopped itself (step 3). The
// agent does not swap itself -- it only stops. The watchdog must notice the
// end before it does anything.
func AgentStopped(rt Runtime) (bool, error) {
	running, _, _, err := rt.Status()
	if err != nil {
		return false, fmt.Errorf("checking whether the agent is running: %w", err)
	}
	return !running, nil
}

// StartDigest starts the revision named in s.Desired (step 4). No digest
// check against a source list here -- see the module docstring.
func StartDigest(rt Runtime, s State) error {
	if err := rt.Start(s.Desired); err != nil {
		return fmt.Errorf("starting digest %q: %w", s.Desired, err)
	}
	return nil
}

// AwaitHealthReport waits, at most 10 minutes, for a health report naming
// s.Desired with a timestamp at or after s.Since (step 5) -- an older, or
// differently digested, report does not count, so a report left behind by
// the previous revision cannot fake a healthy new one (section 22.3). An
// empty reason means healthy; otherwise it already says which of step 5's
// two independent failure paths applied (the deadline, or three restarts
// in a row), ready for RollBackToProven to note as-is. sleep is called
// between polls -- production passes time.Sleep, tests a no-op that just
// counts calls, so the deadline never costs a test real minutes.
func AwaitHealthReport(rt Runtime, healthPath string, s State, sleep func(time.Duration)) (reason string, err error) {
	for i := 0; i < maxHealthPolls; i++ {
		if h, readErr := ReadHealth(healthPath); readErr == nil && h.Digest == s.Desired && h.Timestamp >= s.Since {
			return "", nil
		}
		_, _, restarts, statusErr := rt.Status()
		if statusErr != nil {
			return "", fmt.Errorf("checking restart count: %w", statusErr)
		}
		if restarts >= maxRestarts {
			return "the container restarted three times in a row", nil
		}
		sleep(healthPollInterval)
	}
	return "no health report for the new digest within the deadline", nil
}

// RollBackToProven falls back to s.Proven when the health report fails to
// arrive or after three restarts (step 5), and notes reason on stderr --
// captured by journald under the systemd unit, deliberately not a new file
// format for a single line nobody reads back programmatically. An empty
// s.Proven has not been the normal state since section 22.5: the image
// recipe pre-sets it at build time, so every shipped device has a fallback
// target from the first boot -- reported as an error here, not guessed at,
// and nothing is started.
func RollBackToProven(rt Runtime, s State, reason string) error {
	if s.Proven == "" {
		return fmt.Errorf("no proven digest present -- sign of a faulty delivery, see docs/specification.md section 22.5")
	}
	if err := rt.Start(s.Proven); err != nil {
		return fmt.Errorf("rolling back to %q: %w", s.Proven, err)
	}
	fmt.Fprintf(os.Stderr, "thermoctl-watchdog: rolled back to %s: %s\n", s.Proven, reason)
	return nil
}

// Outcome is what one Reconcile pass decided -- the single hook P5.7 needs
// to map a watchdog state to a blink pattern (section 23) without touching
// this decision again: OutcomeOK covers "nothing to do", "restarted" and "a
// swap proved healthy" alike, since LED 1 (section 23.2) draws no
// distinction between them either -- only whether a rollback just happened.
type Outcome int

const (
	OutcomeOK Outcome = iota
	OutcomeRolledBack
)

// Reconcile runs one pass of steps 3-6 from section 17: notice the agent's
// end, start what is needed, and roll back a pending swap that does not
// prove itself. desired == proven means there is nothing to swap between --
// the agent is simply restarted on the one digest that exists, without the
// await/rollback dance, since there is no different digest to fall back to
// anyway; a failure here is only reported, not rolled back into itself.
func Reconcile(rt Runtime, s State, healthPath string, sleep func(time.Duration)) (Outcome, error) {
	stopped, err := AgentStopped(rt)
	if err != nil || !stopped {
		return OutcomeOK, err
	}
	if err := StartDigest(rt, s); err != nil {
		return OutcomeOK, err
	}
	if s.Desired == s.Proven {
		return OutcomeOK, nil
	}
	reason, err := AwaitHealthReport(rt, healthPath, s, sleep)
	if err != nil {
		return OutcomeOK, err
	}
	if reason == "" {
		return OutcomeOK, nil
	}
	if err := RollBackToProven(rt, s, reason); err != nil {
		return OutcomeRolledBack, err
	}
	return OutcomeRolledBack, nil
}
