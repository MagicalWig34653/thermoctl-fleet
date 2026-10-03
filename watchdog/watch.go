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
// newly started revision must prove itself, anchored to the state file's
// own `since` (section 22.2) rather than to whenever this process happened
// to start -- a watchdog restart mid-window must not grant a fresh 10
// minutes (cross-review R2).
const healthDeadline = 10 * time.Minute

// maxRestarts: three restarts in a row is a failed swap (section 17, step
// 5), independent of the deadline above.
const maxRestarts = 3

// healthStaleAfter: how old a health report may be, relative to *now*, and
// still count as proof of life (section 22.3: "the watchdog checks its
// age, older than 120 seconds counts as silent" -- reused here verbatim,
// not a separately invented bound). Full-review fix: a single health
// report that once matched s.Desired/s.Since used to satisfy
// AwaitHealthReport forever, on every later Reconcile tick, even once the
// revision that wrote it had since crash-looped and gone silent -- this
// bound is what makes "the report is still fresh" an actual, repeated
// check against the clock, not a one-time fact recorded at the moment it
// first appeared.
const healthStaleAfter = 120 * time.Second

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

// AwaitHealthReport waits until s.Since+10 minutes (section 17 step 5,
// 22.2) for a health report naming s.Desired with a timestamp at or after
// s.Since -- an older, or differently digested, report does not count, so a
// report left behind by the previous revision cannot fake a healthy new one
// (section 22.3). Anchored to s.Since, not to whenever this call started:
// resuming mid-window (the watchdog itself restarted) waits only the
// remainder, never a fresh 10 minutes. now/sleep are injected so tests
// never wait on a real clock -- production passes time.Now/time.Sleep.
//
// **Full-review fix, two parts, both needed together:**
//
//  1. **The restart count is checked before the health report, not
//     after.** The previous order returned "healthy" the moment any
//     single matching report was found, before ever looking at restarts
//     again -- so a revision that wrote one good report and then started
//     crash-looping was never rolled back, because the health check kept
//     winning the race against the restart check on every later call.
//     Checking restarts first means three restarts roll back immediately,
//     regardless of what the health file still says.
//  2. **The report must also be fresh relative to *now*, not only
//     relative to s.Since** (healthStaleAfter, section 22.3's own 120
//     seconds). Without this, a report written once, right after the
//     swap, and never updated again (the writing process died without
//     restarting the container -- so the restart-count check above never
//     fires either) would go on satisfying "healthy" forever: this
//     function is called fresh on every Reconcile tick as long as
//     s.Desired != s.Proven, and the old check only ever asked "is this
//     the right digest, from on-or-after the swap" -- never "is this
//     still happening". A report older than healthStaleAfter is treated
//     exactly like a missing one.
func AwaitHealthReport(rt Runtime, healthPath string, s State, now func() time.Time, sleep func(time.Duration)) (reason string, err error) {
	deadline := time.Unix(s.Since, 0).Add(healthDeadline)
	for now().Before(deadline) {
		_, _, restarts, statusErr := rt.Status()
		if statusErr != nil {
			return "", fmt.Errorf("checking restart count: %w", statusErr)
		}
		if restarts >= maxRestarts {
			return "the container restarted three times in a row", nil
		}
		if h, readErr := ReadHealth(healthPath); readErr == nil &&
			h.Digest == s.Desired &&
			h.Timestamp >= s.Since &&
			now().Sub(time.Unix(h.Timestamp, 0)) <= healthStaleAfter {
			return "", nil
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
// end (step 3 -- read once, here, as part of the same Status call that
// also needs the running digest for the check below, rather than through a
// separate, now-unused "AgentStopped" that could only ever answer half the
// question), start what is needed, and roll back a pending swap that does
// not prove itself -- or, if the agent (and this process) were already
// running mid-swap when this pass began, resume waiting for the remainder
// of the deadline instead of assuming a running container means "done"
// (cross-review R2: a watchdog restart during the 10-minute window must
// not let an unhealthy, unproven revision stand forever).
// A container running anything other than s.Desired (stale or foreign) is
// left alone, same as desired == proven: both fall through to the shared
// "nothing pending" check below without touching the runtime again.
// desired == proven means there is nothing to swap between -- the agent is
// simply started when stopped, without the await/rollback dance; a start
// failure there is only reported, not rolled back into itself (there is
// nothing else to try).
func Reconcile(rt Runtime, s State, healthPath string, now func() time.Time, sleep func(time.Duration)) (Outcome, error) {
	running, runningDigest, _, err := rt.Status()
	if err != nil {
		return OutcomeOK, fmt.Errorf("checking whether the agent is running: %w", err)
	}
	if !running {
		if err := rt.Start(s.Desired); err != nil {
			return OutcomeOK, fmt.Errorf("starting digest %q: %w", s.Desired, err)
		}
	} else if runningDigest != s.Desired {
		return OutcomeOK, nil
	}
	if s.Desired == s.Proven {
		return OutcomeOK, nil
	}
	reason, err := AwaitHealthReport(rt, healthPath, s, now, sleep)
	if err != nil || reason == "" {
		return OutcomeOK, err
	}
	return OutcomeRolledBack, RollBackToProven(rt, s, reason)
}
