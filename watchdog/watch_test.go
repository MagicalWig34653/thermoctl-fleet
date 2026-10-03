package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fakeRuntime substitutes the real container runtime (section 18.3): no
// exec.Command, no Docker, just what the test wants Status/Start to report.
type fakeRuntime struct {
	running     bool
	digest      string
	restarts    int
	statusErr   error
	startErr    map[string]error // digest -> error, nil entry means "succeeds"
	startedFor  []string         // every digest Start was called with, in order
	afterStart  func()           // runs once, after the first successful Start (simulates "container came up")
	startedOnce bool
}

func (f *fakeRuntime) Status() (bool, string, int, error) {
	return f.running, f.digest, f.restarts, f.statusErr
}

func (f *fakeRuntime) Start(digest string) error {
	f.startedFor = append(f.startedFor, digest)
	if err, failed := f.startErr[digest]; failed && err != nil {
		return err
	}
	if !f.startedOnce {
		f.startedOnce = true
		if f.afterStart != nil {
			f.afterStart()
		}
	}
	return nil
}

// testNow anchors every test's notion of "the current time" -- Since
// values are computed relative to it, never to a real clock (cross-review
// R2: AwaitHealthReport's deadline is now anchored to Since, so tests must
// keep Since and "now" consistent with each other, not just with zero).
var testNow = time.Unix(1_700_000_000, 0)

// fakeClock lets a test control both Now and Sleep from one shared,
// virtual point in time -- sleep advances it, now reads it -- so the
// 10-minute deadline is exercised deterministically, never a real wait.
type fakeClock struct {
	t      time.Time
	sleeps int
}

func newFakeClock() *fakeClock { return &fakeClock{t: testNow} }

func (c *fakeClock) now() time.Time        { return c.t }
func (c *fakeClock) sleep(d time.Duration) { c.t = c.t.Add(d); c.sleeps++ }

// fullWindowPolls is how many times AwaitHealthReport sleeps while waiting
// out a fresh, un-resumed 10-minute deadline -- used to tell "waited the
// remainder" apart from "waited a fresh window" in the resumability tests.
const fullWindowPolls = int(10 * time.Minute / healthPollInterval)

func writeHealth(t *testing.T, digest string, timestamp int64) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "health.env")
	content := fmt.Sprintf("timestamp=%d\ndigest=%s\nversion=0.4.0\n", timestamp, digest)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("setup failed: %v", err)
	}
	return path
}

func TestReconcileRuntimeStatusErrorIsReported(t *testing.T) {
	// Step 3 ("has the agent stopped?") is now read as part of Reconcile's
	// own Status call rather than through a separate AgentStopped -- this
	// is that error path's test.
	rt := &fakeRuntime{statusErr: errors.New("runtime unreachable")}
	s := State{Desired: "sha256:new", Proven: "sha256:old"}
	clock := newFakeClock()

	if _, err := Reconcile(rt, s, "unused", clock.now, clock.sleep); err == nil {
		t.Fatal("expected error did not occur")
	}
}

// Starting the desired digest itself (step 4) is exercised through
// Reconcile now (no more separate StartDigest): the success path by
// TestReconcileDesiredEqualsProvenJustRestarts and TestReconcileSwapSucceeds,
// the failure path by TestReconcileStartFailureIsReportedNotRolledBack
// below.

func TestAwaitHealthReportHappyPath(t *testing.T) {
	since := testNow.Unix()
	health := writeHealth(t, "sha256:new", since)
	rt := &fakeRuntime{}
	clock := newFakeClock()

	reason, err := AwaitHealthReport(rt, health, State{Desired: "sha256:new", Since: since}, clock.now, clock.sleep)
	if err != nil || reason != "" {
		t.Fatalf("reason=%q err=%v, expected healthy", reason, err)
	}
	if clock.sleeps != 0 {
		t.Errorf("slept %d times, expected an immediate match with no sleep", clock.sleeps)
	}
}

func TestAwaitHealthReportMissingRollsBackAfterDeadline(t *testing.T) {
	since := testNow.Unix()
	missing := filepath.Join(t.TempDir(), "never-written.env")
	rt := &fakeRuntime{}
	clock := newFakeClock()

	reason, err := AwaitHealthReport(rt, missing, State{Desired: "sha256:new", Since: since}, clock.now, clock.sleep)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty reason, got healthy")
	}
	if clock.sleeps != fullWindowPolls {
		t.Errorf("slept %d times, expected the full %d polls of a fresh deadline", clock.sleeps, fullWindowPolls)
	}
}

func TestAwaitHealthReportWrongDigestTreatedAsMissing(t *testing.T) {
	// A health report left behind by the previous revision must not fake a
	// healthy new one (section 22.3).
	since := testNow.Unix()
	health := writeHealth(t, "sha256:old", since+999)
	rt := &fakeRuntime{}
	clock := newFakeClock()

	reason, err := AwaitHealthReport(rt, health, State{Desired: "sha256:new", Since: since}, clock.now, clock.sleep)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty reason for the wrong digest, got healthy")
	}
}

func TestAwaitHealthReportStaleTimestampTreatedAsMissing(t *testing.T) {
	// A report older than Since is left behind by the revision being
	// replaced, not proof the new one is alive (section 22.2).
	since := testNow.Unix()
	health := writeHealth(t, "sha256:new", since-1)
	rt := &fakeRuntime{}
	clock := newFakeClock()

	reason, _ := AwaitHealthReport(rt, health, State{Desired: "sha256:new", Since: since}, clock.now, clock.sleep)
	if reason == "" {
		t.Fatal("expected a non-empty reason for the stale timestamp, got healthy")
	}
}

func TestAwaitHealthReportThreeRestartsRollsBackImmediately(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "never-written.env")
	rt := &fakeRuntime{restarts: 3}
	clock := newFakeClock()

	reason, err := AwaitHealthReport(rt, missing, State{Desired: "sha256:new", Since: testNow.Unix()}, clock.now, clock.sleep)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty reason for the restart loop, got healthy")
	}
	if clock.sleeps != 0 {
		t.Errorf("slept %d times, expected an immediate rollback with no sleep", clock.sleeps)
	}
}

func TestAwaitHealthReportPropagatesRuntimeError(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "never-written.env")
	rt := &fakeRuntime{statusErr: errors.New("runtime unreachable")}
	clock := newFakeClock()

	if _, err := AwaitHealthReport(rt, missing, State{Desired: "sha256:new", Since: testNow.Unix()}, clock.now, clock.sleep); err == nil {
		t.Fatal("expected error did not occur")
	}
}

// Cross-review R2: the deadline is anchored to Since, not to whenever this
// call happens to start -- a watchdog restart mid-window must resume with
// only the time actually left, not a fresh 10 minutes.

func TestAwaitHealthReportDeadlineAlreadyPassedRollsBackImmediately(t *testing.T) {
	since := testNow.Add(-11 * time.Minute).Unix() // 10-minute deadline already elapsed a minute ago
	missing := filepath.Join(t.TempDir(), "never-written.env")
	rt := &fakeRuntime{}
	clock := newFakeClock()

	reason, err := AwaitHealthReport(rt, missing, State{Desired: "sha256:new", Since: since}, clock.now, clock.sleep)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty reason, got healthy")
	}
	if clock.sleeps != 0 {
		t.Errorf("slept %d times, expected zero -- the deadline was already behind us", clock.sleeps)
	}
}

func TestAwaitHealthReportWaitsOnlyTheRemainderWhenResumed(t *testing.T) {
	since := testNow.Add(-9 * time.Minute).Unix() // 1 minute of the 10 left
	missing := filepath.Join(t.TempDir(), "never-written.env")
	rt := &fakeRuntime{}
	clock := newFakeClock()

	reason, err := AwaitHealthReport(rt, missing, State{Desired: "sha256:new", Since: since}, clock.now, clock.sleep)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty reason, got healthy")
	}
	if clock.sleeps == 0 || clock.sleeps >= fullWindowPolls {
		t.Errorf("slept %d times, expected only the ~1-minute remainder, well under a fresh %d", clock.sleeps, fullWindowPolls)
	}
}

func TestRollBackToProvenWithoutProvenState(t *testing.T) {
	rt := &fakeRuntime{}
	if err := RollBackToProven(rt, State{Desired: "sha256:9f2c"}, "test"); err == nil {
		t.Fatal("expected error did not occur")
	}
	if len(rt.startedFor) != 0 {
		t.Fatalf("started %v, expected no action taken", rt.startedFor)
	}
}

func TestRollBackToProvenStartsProven(t *testing.T) {
	rt := &fakeRuntime{}
	s := State{Desired: "sha256:9f2c", Proven: "sha256:1a7b"}
	if err := RollBackToProven(rt, s, "no health report"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(rt.startedFor) != 1 || rt.startedFor[0] != "sha256:1a7b" {
		t.Fatalf("started %v, expected exactly [sha256:1a7b]", rt.startedFor)
	}
}

func TestRollBackToProvenReportsRuntimeFailure(t *testing.T) {
	rt := &fakeRuntime{startErr: map[string]error{"sha256:1a7b": errors.New("no such image")}}
	s := State{Desired: "sha256:9f2c", Proven: "sha256:1a7b"}
	if err := RollBackToProven(rt, s, "no health report"); err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestReconcileAgentRunningSomethingElseLeftAlone(t *testing.T) {
	rt := &fakeRuntime{running: true, digest: "sha256:unrelated"}
	s := State{Desired: "sha256:new", Proven: "sha256:old"}
	clock := newFakeClock()

	outcome, err := Reconcile(rt, s, "unused", clock.now, clock.sleep)
	if err != nil || outcome != OutcomeOK {
		t.Fatalf("outcome=%v err=%v, expected OutcomeOK/nil", outcome, err)
	}
	if len(rt.startedFor) != 0 {
		t.Fatalf("started %v, expected no action", rt.startedFor)
	}
}

func TestReconcileDesiredEqualsProvenJustRestarts(t *testing.T) {
	rt := &fakeRuntime{running: false}
	s := State{Desired: "sha256:same", Proven: "sha256:same"}
	clock := newFakeClock()

	outcome, err := Reconcile(rt, s, "unused", clock.now, clock.sleep)
	if err != nil || outcome != OutcomeOK {
		t.Fatalf("outcome=%v err=%v, expected OutcomeOK/nil", outcome, err)
	}
	if len(rt.startedFor) != 1 || rt.startedFor[0] != "sha256:same" {
		t.Fatalf("started %v, expected exactly one start of sha256:same", rt.startedFor)
	}
	if clock.sleeps != 0 {
		t.Errorf("slept %d times, expected no await/rollback dance -- nothing to swap", clock.sleeps)
	}
}

func TestReconcileSwapSucceeds(t *testing.T) {
	dir := t.TempDir()
	health := filepath.Join(dir, "health.env")
	since := testNow.Unix()
	rt := &fakeRuntime{running: false}
	rt.afterStart = func() {
		content := fmt.Sprintf("timestamp=%d\ndigest=sha256:new\nversion=0.5.0\n", since)
		if err := os.WriteFile(health, []byte(content), 0o600); err != nil {
			t.Fatalf("setup failed: %v", err)
		}
	}
	s := State{Desired: "sha256:new", Proven: "sha256:old", Since: since}
	clock := newFakeClock()

	outcome, err := Reconcile(rt, s, health, clock.now, clock.sleep)
	if err != nil || outcome != OutcomeOK {
		t.Fatalf("outcome=%v err=%v, expected OutcomeOK/nil", outcome, err)
	}
	if len(rt.startedFor) != 1 || rt.startedFor[0] != "sha256:new" {
		t.Fatalf("started %v, expected exactly one start of sha256:new, no rollback", rt.startedFor)
	}
}

func TestReconcileRollsBackWhenHealthNeverArrives(t *testing.T) {
	rt := &fakeRuntime{running: false}
	s := State{Desired: "sha256:new", Proven: "sha256:old", Since: testNow.Unix()}
	clock := newFakeClock()

	outcome, err := Reconcile(rt, s, filepath.Join(t.TempDir(), "missing.env"), clock.now, clock.sleep)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if outcome != OutcomeRolledBack {
		t.Fatalf("outcome=%v, expected OutcomeRolledBack", outcome)
	}
	if len(rt.startedFor) != 2 || rt.startedFor[0] != "sha256:new" || rt.startedFor[1] != "sha256:old" {
		t.Fatalf("started %v, expected [sha256:new sha256:old]", rt.startedFor)
	}
}

func TestReconcileRollsBackAfterThreeRestarts(t *testing.T) {
	rt := &fakeRuntime{running: false, restarts: 3}
	s := State{Desired: "sha256:new", Proven: "sha256:old", Since: testNow.Unix()}
	clock := newFakeClock()

	outcome, err := Reconcile(rt, s, filepath.Join(t.TempDir(), "missing.env"), clock.now, clock.sleep)
	if err != nil || outcome != OutcomeRolledBack {
		t.Fatalf("outcome=%v err=%v, expected OutcomeRolledBack/nil", outcome, err)
	}
}

func TestReconcileEmptyProvenReportsErrorWithoutAction(t *testing.T) {
	rt := &fakeRuntime{running: false}
	s := State{Desired: "sha256:new", Proven: "", Since: testNow.Unix()}
	clock := newFakeClock()

	outcome, err := Reconcile(rt, s, filepath.Join(t.TempDir(), "missing.env"), clock.now, clock.sleep)
	if err == nil {
		t.Fatal("expected error did not occur")
	}
	if outcome != OutcomeRolledBack {
		t.Fatalf("outcome=%v, expected OutcomeRolledBack (attempted, failed)", outcome)
	}
	if len(rt.startedFor) != 1 || rt.startedFor[0] != "sha256:new" {
		t.Fatalf("started %v, expected only the failed swap attempt, no fallback start", rt.startedFor)
	}
}

func TestReconcileStartFailureIsReportedNotRolledBack(t *testing.T) {
	// desired == proven: there is nothing else to fall back to, so a failed
	// start is only reported -- see Reconcile's own comment.
	rt := &fakeRuntime{running: false, startErr: map[string]error{"sha256:same": errors.New("no such image")}}
	s := State{Desired: "sha256:same", Proven: "sha256:same"}
	clock := newFakeClock()

	outcome, err := Reconcile(rt, s, "unused", clock.now, clock.sleep)
	if err == nil {
		t.Fatal("expected error did not occur")
	}
	if outcome != OutcomeOK {
		t.Fatalf("outcome=%v, expected OutcomeOK (reported, not rolled back)", outcome)
	}
	if len(rt.startedFor) != 1 {
		t.Fatalf("started %v, expected exactly one attempt, no crash loop", rt.startedFor)
	}
}

// Cross-review R2: the watchdog itself may have restarted mid-swap -- a
// running container on s.Desired must not be mistaken for "done" without
// checking whether it has actually proved itself yet.

func TestReconcileResumedMidSwapPastDeadlineRollsBack(t *testing.T) {
	rt := &fakeRuntime{running: true, digest: "sha256:new"}
	s := State{Desired: "sha256:new", Proven: "sha256:old", Since: testNow.Add(-11 * time.Minute).Unix()}
	clock := newFakeClock()

	outcome, err := Reconcile(rt, s, filepath.Join(t.TempDir(), "missing.env"), clock.now, clock.sleep)
	if err != nil || outcome != OutcomeRolledBack {
		t.Fatalf("outcome=%v err=%v, expected OutcomeRolledBack/nil", outcome, err)
	}
	if len(rt.startedFor) != 1 || rt.startedFor[0] != "sha256:old" {
		t.Fatalf("started %v, expected only the rollback start to proven -- desired was already running", rt.startedFor)
	}
	if clock.sleeps != 0 {
		t.Errorf("slept %d times, expected an immediate rollback -- the deadline was already behind us", clock.sleeps)
	}
}

func TestReconcileResumedMidSwapWithTimeRemainingFindsHealthy(t *testing.T) {
	since := testNow.Add(-9 * time.Minute).Unix() // 1 minute of the 10 left
	// Fresh relative to "now" (testNow), not just relative to since --
	// full-review fix: a report must satisfy both.
	health := writeHealth(t, "sha256:new", testNow.Unix()-30)
	rt := &fakeRuntime{running: true, digest: "sha256:new"}
	s := State{Desired: "sha256:new", Proven: "sha256:old", Since: since}
	clock := newFakeClock()

	outcome, err := Reconcile(rt, s, health, clock.now, clock.sleep)
	if err != nil || outcome != OutcomeOK {
		t.Fatalf("outcome=%v err=%v, expected OutcomeOK/nil", outcome, err)
	}
	if len(rt.startedFor) != 0 {
		t.Fatalf("started %v, expected no action -- it was already running and proved healthy", rt.startedFor)
	}
}

// Full-review fix: the restart count must be checked on every poll, not
// only before the first health report is ever seen -- a revision that
// wrote one good report and then started crash-looping must still be
// rolled back while desired != proven.

func TestAwaitHealthReportCrashLoopAfterOneGoodReportRollsBack(t *testing.T) {
	since := testNow.Unix()
	health := writeHealth(t, "sha256:new", since)
	rt := &fakeRuntime{restarts: 0}
	clock := newFakeClock()

	// First poll: fresh, matching report, no restarts yet -- healthy.
	reason, err := AwaitHealthReport(rt, health, State{Desired: "sha256:new", Since: since}, clock.now, clock.sleep)
	if err != nil || reason != "" {
		t.Fatalf("reason=%q err=%v, expected healthy on the first check", reason, err)
	}

	// The same revision now starts crash-looping: the health report on
	// disk is untouched (the old version under the old logic), but the
	// restart count has since climbed to the threshold.
	rt.restarts = maxRestarts
	reason, err = AwaitHealthReport(rt, health, State{Desired: "sha256:new", Since: since}, clock.now, clock.sleep)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty rollback reason once restarts hit the threshold, got healthy")
	}
}

// Full-review fix: a report that matches digest and Since but has not
// been refreshed recently (relative to now) must not count as healthy
// forever -- the writing process may have died without the container
// itself restarting.

func TestAwaitHealthReportStaleRelativeToNowTreatedAsMissing(t *testing.T) {
	since := testNow.Add(-5 * time.Minute).Unix() // still well within the 10-minute deadline
	// Matches digest and is >= Since, but was written long before "now".
	health := writeHealth(t, "sha256:new", since)
	rt := &fakeRuntime{}
	clock := newFakeClock() // now == testNow, i.e. 5 minutes after the report's own timestamp

	reason, err := AwaitHealthReport(rt, health, State{Desired: "sha256:new", Since: since}, clock.now, clock.sleep)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty reason for the stale-relative-to-now report, got healthy")
	}
}
