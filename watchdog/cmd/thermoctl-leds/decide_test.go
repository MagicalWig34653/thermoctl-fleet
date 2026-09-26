package main

import (
	"testing"
	"time"

	"github.com/magicalwig34653/thermoctl-fleet/watchdog/internal/ledsysfs"
)

// A fixed instant, well past the Unix epoch, so "now minus an interval"
// never goes negative in these tests.
var testNow = time.Unix(2_000_000_000, 0)

const testStaleAfter = 360 * time.Second

func healthyBaseline() Inputs {
	return Inputs{
		Now:        testNow,
		StaleAfter: testStaleAfter,
		State:      &WatchdogState{Desired: "sha256:" + "a", Since: testNow.Add(-1 * time.Hour).Unix()},
		Health: &HealthReport{
			Timestamp: testNow.Add(-10 * time.Second).Unix(),
			Digest:    "sha256:" + "a",
		},
		AgentStatus: &AgentStatus{
			Timestamp:    testNow.Add(-10 * time.Second).Unix(),
			CloudContact: "ok",
			Fault:        "none",
			Control:      "ok",
		},
	}
}

// TestDecideDeviceEveryState covers every pattern section 23.2 defines for
// LED 1, plus the precedence and staleness rules layered on top of it.
func TestDecideDeviceEveryState(t *testing.T) {
	cases := []struct {
		name string
		in   func() Inputs
		want ledsysfs.Pattern
	}{
		{
			name: "healthy, cloud contact ok -> steady on (the documented baseline)",
			in:   healthyBaseline,
			want: ledsysfs.SteadyOn,
		},
		{
			name: "no state, no health, nothing written yet -> slow blink",
			in:   func() Inputs { return Inputs{Now: testNow, StaleAfter: testStaleAfter} },
			want: ledsysfs.SlowBlink,
		},
		{
			name: "waiting for assignment overrides an otherwise-healthy report",
			in: func() Inputs {
				in := healthyBaseline()
				in.Registration = &RegistrationStatus{Status: "waiting_for_assignment"}
				return in
			},
			want: ledsysfs.FastBlink,
		},
		{
			name: "assigned (not waiting) does not trigger fast blink",
			in: func() Inputs {
				in := healthyBaseline()
				in.Registration = &RegistrationStatus{Status: "assigned"}
				return in
			},
			want: ledsysfs.SteadyOn,
		},
		{
			name: "health report names a digest other than desired -> not healthy",
			in: func() Inputs {
				in := healthyBaseline()
				in.Health.Digest = "sha256:" + "different"
				return in
			},
			want: ledsysfs.SlowBlink,
		},
		{
			name: "health report predates the current desired revision (section 22.3)",
			in: func() Inputs {
				in := healthyBaseline()
				in.Health.Timestamp = in.State.Since - 1
				return in
			},
			want: ledsysfs.SlowBlink,
		},
		{
			name: "health report older than the staleness window -> unknown, not steady-on",
			in: func() Inputs {
				in := healthyBaseline()
				in.Health.Timestamp = testNow.Add(-1 * time.Hour).Unix()
				return in
			},
			want: ledsysfs.SlowBlink,
		},
		{
			name: "agent status file missing even though health is fine -> unknown",
			in: func() Inputs {
				in := healthyBaseline()
				in.AgentStatus = nil
				return in
			},
			want: ledsysfs.SlowBlink,
		},
		{
			name: "agent status file stale -> unknown, never a stale steady-on",
			in: func() Inputs {
				in := healthyBaseline()
				in.AgentStatus.Timestamp = testNow.Add(-1 * time.Hour).Unix()
				return in
			},
			want: ledsysfs.SlowBlink,
		},
		{
			name: "healthy but no cloud contact -> two short blinks",
			in: func() Inputs {
				in := healthyBaseline()
				in.AgentStatus.CloudContact = "lost"
				return in
			},
			want: ledsysfs.TwoShortBlinks,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := DecideDevice(c.in()); got != c.want {
				t.Errorf("DecideDevice() = %q, want %q", got, c.want)
			}
		})
	}
}

// TestDecideSystemEveryState covers every pattern section 23.2 defines for
// LED 2, plus the control-vs-fault precedence and the staleness rule.
func TestDecideSystemEveryState(t *testing.T) {
	cases := []struct {
		name string
		in   func() Inputs
		want ledsysfs.Pattern
	}{
		{
			name: "no open fault, control fine -> off (the documented baseline)",
			in:   healthyBaseline,
			want: ledsysfs.Off,
		},
		{
			name: "open fault -> slow blink",
			in: func() Inputs {
				in := healthyBaseline()
				in.AgentStatus.Fault = "open"
				return in
			},
			want: ledsysfs.SlowBlink,
		},
		{
			name: "control stalled -> steady on",
			in: func() Inputs {
				in := healthyBaseline()
				in.AgentStatus.Control = "stalled"
				return in
			},
			want: ledsysfs.SteadyOn,
		},
		{
			name: "control stalled ranks above a merely open fault",
			in: func() Inputs {
				in := healthyBaseline()
				in.AgentStatus.Control = "stalled"
				in.AgentStatus.Fault = "open"
				return in
			},
			want: ledsysfs.SteadyOn,
		},
		{
			name: "agent status file missing -> unknown, conservative slow blink",
			in: func() Inputs {
				in := healthyBaseline()
				in.AgentStatus = nil
				return in
			},
			want: ledsysfs.SlowBlink,
		},
		{
			name: "agent status file stale -> unknown, conservative slow blink",
			in: func() Inputs {
				in := healthyBaseline()
				in.AgentStatus.Timestamp = testNow.Add(-1 * time.Hour).Unix()
				return in
			},
			want: ledsysfs.SlowBlink,
		},
		{
			name: "LED 2 does not react to 'waiting for assignment' (not in its own table)",
			in: func() Inputs {
				in := healthyBaseline()
				in.Registration = &RegistrationStatus{Status: "waiting_for_assignment"}
				return in
			},
			want: ledsysfs.Off,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := DecideSystem(c.in()); got != c.want {
				t.Errorf("DecideSystem() = %q, want %q", got, c.want)
			}
		})
	}
}
