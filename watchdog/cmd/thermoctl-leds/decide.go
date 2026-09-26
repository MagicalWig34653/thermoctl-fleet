// Maps the four input files (inputs.go) to the blink patterns section
// 23.2 defines, with precedence rules for when several conditions apply at
// once, and the staleness rule (docs/STATUS.md's P5.7 entry): a periodic
// report (health, agent status) older than StaleAfter is treated as
// unknown, never as "still good".
package main

import (
	"time"

	"github.com/magicalwig34653/thermoctl-fleet/watchdog/internal/ledsysfs"
)

// Inputs bundles everything one decision pass needs. now is injected (not
// time.Now directly) so tests never depend on a real clock, the same
// pattern watchdog/watch.go uses for AwaitHealthReport/Reconcile.
type Inputs struct {
	Now          time.Time
	StaleAfter   time.Duration
	State        *WatchdogState
	Health       *HealthReport
	Registration *RegistrationStatus
	AgentStatus  *AgentStatus
}

// fresh reports whether timestamp is within StaleAfter of Now -- shared by
// both periodic reports (health, agent status). A file that fails to
// parse or does not exist yet is represented as a nil pointer upstream,
// not as a zero timestamp reaching this function, so this only ever
// judges an actually-read timestamp's age.
func (in Inputs) fresh(timestamp int64) bool {
	age := in.Now.Sub(time.Unix(timestamp, 0))
	return age >= 0 && age <= in.StaleAfter
}

// agentHealthy reproduces, from local files alone, the same judgement
// watchdog/watch.go::AwaitHealthReport makes from inside the watchdog
// process (h.Digest == s.Desired && h.Timestamp >= s.Since): the health
// report must be fresh, must name the *currently desired* digest -- not
// merely "some" digest -- and must not predate the point since which that
// digest has been desired (an older report left over from the previous
// revision must not fake a healthy new one, section 22.3). A watchdog
// state file or health report that is missing entirely (not yet written,
// or removed) counts as "not healthy" here, the same conservative
// direction as an aged-out one.
func agentHealthy(in Inputs) bool {
	if in.State == nil || in.Health == nil {
		return false
	}
	if !in.fresh(in.Health.Timestamp) {
		return false
	}
	if in.Health.Digest != in.State.Desired {
		return false
	}
	return in.Health.Timestamp >= in.State.Since
}

// agentStatusFresh reports whether the agent's own status file (the one
// only the agent can know: cloud contact, open fault, stalled control)
// is present and recent enough to trust.
func agentStatusFresh(in Inputs) bool {
	return in.AgentStatus != nil && in.fresh(in.AgentStatus.Timestamp)
}

// waitingForAssignment reads P5.0's registration status file. No
// staleness window applies to it (unlike Health/AgentStatus): it is
// written on real state transitions (registered, assigned), not on a
// periodic cadence, so there is no "heartbeat interval" to measure its
// age against -- a device can legitimately sit in "waiting_for_assignment"
// for days without a fresh write, and that must still show the fast-blink
// pattern, not fall back to "unknown" the way an aged-out health report
// would.
func waitingForAssignment(in Inputs) bool {
	return in.Registration != nil && in.Registration.Status == "waiting_for_assignment"
}

// DecideDevice implements LED 1's table from section 23.2, most specific
// condition first:
//
//  1. Waiting for assignment (section 15.3) overrides everything else --
//     nothing about "healthy" or "cloud contact" is meaningful before a
//     device has even been assigned to an apartment.
//  2. The agent is not (yet, or provably) healthy, or its own status file
//     is missing/stale -> the conservative "slow blink" (section 23.2:
//     "starting, or the agent is not yet healthy"), which is also this
//     program's answer to "we do not actually know" per the staleness
//     rule -- never a stale "steady on".
//  3. Healthy, but the agent itself reports no cloud contact -> "two short
//     blinks, pause".
//  4. Healthy and cloud contact ok -> "steady on".
func DecideDevice(in Inputs) ledsysfs.Pattern {
	if waitingForAssignment(in) {
		return ledsysfs.FastBlink
	}
	if !agentHealthy(in) {
		return ledsysfs.SlowBlink
	}
	if !agentStatusFresh(in) {
		return ledsysfs.SlowBlink
	}
	if in.AgentStatus.CloudContact == "lost" {
		return ledsysfs.TwoShortBlinks
	}
	return ledsysfs.SteadyOn
}

// DecideSystem implements LED 2's table from section 23.2, which section
// 23.2 defines purely from what the agent itself knows (fault, control) --
// unlike LED 1, it draws no distinction for "waiting for assignment" or
// "no cloud contact", so this function looks only at the agent status
// file:
//
//  1. Missing/stale agent status -> unknown, and the conservative choice
//     here is to reuse the existing "slow blink" (open fault) pattern
//     rather than silently falling back to "off", which would itself read
//     as a stale "no fault" -- section 23.2 defines no fourth, dedicated
//     "unknown" pattern for this LED, so the more cautious of the three
//     existing ones is chosen instead of inventing one (documented
//     decision, docs/STATUS.md's P5.7 entry).
//  2. Control stalled ("no decision for over three cycles") -> steady on,
//     ranked above a merely open fault: a stalled control loop is the more
//     severe of the two conditions the agent can report on this LED.
//  3. Open fault (sensor, bridge, switching command) -> slow blink.
//  4. Otherwise -> off (the documented baseline, "yellow off").
func DecideSystem(in Inputs) ledsysfs.Pattern {
	if !agentStatusFresh(in) {
		return ledsysfs.SlowBlink
	}
	if in.AgentStatus.Control == "stalled" {
		return ledsysfs.SteadyOn
	}
	if in.AgentStatus.Fault == "open" {
		return ledsysfs.SlowBlink
	}
	return ledsysfs.Off
}
