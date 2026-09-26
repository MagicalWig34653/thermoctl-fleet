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

// noSleep counts calls instead of ever actually waiting -- AwaitHealthReport's
// 10-minute deadline must never cost a test real time.
func noSleep(calls *int) func(time.Duration) {
	return func(time.Duration) { *calls++ }
}

func writeHealth(t *testing.T, digest string, timestamp int64) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "health.env")
	content := fmt.Sprintf("timestamp=%d\ndigest=%s\nversion=0.4.0\n", timestamp, digest)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("setup failed: %v", err)
	}
	return path
}

func TestAgentStoppedReportsRunning(t *testing.T) {
	rt := &fakeRuntime{running: true}
	stopped, err := AgentStopped(rt)
	if err != nil || stopped {
		t.Fatalf("stopped=%v err=%v, expected not stopped", stopped, err)
	}
}

func TestAgentStoppedReportsStopped(t *testing.T) {
	rt := &fakeRuntime{running: false}
	stopped, err := AgentStopped(rt)
	if err != nil || !stopped {
		t.Fatalf("stopped=%v err=%v, expected stopped", stopped, err)
	}
}

func TestAgentStoppedPropagatesRuntimeError(t *testing.T) {
	rt := &fakeRuntime{statusErr: errors.New("runtime unreachable")}
	if _, err := AgentStopped(rt); err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestStartDigestStartsDesired(t *testing.T) {
	rt := &fakeRuntime{}
	if err := StartDigest(rt, State{Desired: "sha256:9f2c"}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(rt.startedFor) != 1 || rt.startedFor[0] != "sha256:9f2c" {
		t.Fatalf("started %v, expected exactly [sha256:9f2c]", rt.startedFor)
	}
}

func TestStartDigestReportsRuntimeFailure(t *testing.T) {
	rt := &fakeRuntime{startErr: map[string]error{"sha256:9f2c": errors.New("no such image")}}
	if err := StartDigest(rt, State{Desired: "sha256:9f2c"}); err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestAwaitHealthReportHappyPath(t *testing.T) {
	health := writeHealth(t, "sha256:new", 1000)
	rt := &fakeRuntime{}
	sleeps := 0

	reason, err := AwaitHealthReport(rt, health, State{Desired: "sha256:new", Since: 500}, noSleep(&sleeps))
	if err != nil || reason != "" {
		t.Fatalf("reason=%q err=%v, expected healthy", reason, err)
	}
	if sleeps != 0 {
		t.Errorf("slept %d times, expected an immediate match with no sleep", sleeps)
	}
}

func TestAwaitHealthReportMissingRollsBackAfterDeadline(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "never-written.env")
	rt := &fakeRuntime{}
	sleeps := 0

	reason, err := AwaitHealthReport(rt, missing, State{Desired: "sha256:new", Since: 500}, noSleep(&sleeps))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty reason, got healthy")
	}
	if sleeps != maxHealthPolls {
		t.Errorf("slept %d times, expected the full %d polls of the deadline", sleeps, maxHealthPolls)
	}
}

func TestAwaitHealthReportWrongDigestTreatedAsMissing(t *testing.T) {
	// A health report left behind by the previous revision must not fake a
	// healthy new one (section 22.3).
	health := writeHealth(t, "sha256:old", 999999)
	rt := &fakeRuntime{}
	sleeps := 0

	reason, err := AwaitHealthReport(rt, health, State{Desired: "sha256:new", Since: 500}, noSleep(&sleeps))
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
	health := writeHealth(t, "sha256:new", 100)
	rt := &fakeRuntime{}
	sleeps := 0

	reason, _ := AwaitHealthReport(rt, health, State{Desired: "sha256:new", Since: 500}, noSleep(&sleeps))
	if reason == "" {
		t.Fatal("expected a non-empty reason for the stale timestamp, got healthy")
	}
}

func TestAwaitHealthReportThreeRestartsRollsBackImmediately(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "never-written.env")
	rt := &fakeRuntime{restarts: 3}
	sleeps := 0

	reason, err := AwaitHealthReport(rt, missing, State{Desired: "sha256:new"}, noSleep(&sleeps))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if reason == "" {
		t.Fatal("expected a non-empty reason for the restart loop, got healthy")
	}
	if sleeps != 0 {
		t.Errorf("slept %d times, expected an immediate rollback with no sleep", sleeps)
	}
}

func TestAwaitHealthReportPropagatesRuntimeError(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "never-written.env")
	rt := &fakeRuntime{statusErr: errors.New("runtime unreachable")}
	sleeps := 0

	if _, err := AwaitHealthReport(rt, missing, State{Desired: "sha256:new"}, noSleep(&sleeps)); err == nil {
		t.Fatal("expected error did not occur")
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

func TestReconcileAgentStillRunningDoesNothing(t *testing.T) {
	rt := &fakeRuntime{running: true}
	s := State{Desired: "sha256:new", Proven: "sha256:old"}

	outcome, err := Reconcile(rt, s, "unused", noSleep(new(int)))
	if err != nil || outcome != OutcomeOK {
		t.Fatalf("outcome=%v err=%v, expected OutcomeOK/nil", outcome, err)
	}
	if len(rt.startedFor) != 0 {
		t.Fatalf("started %v, expected no action while the agent is running", rt.startedFor)
	}
}

func TestReconcileDesiredEqualsProvenJustRestarts(t *testing.T) {
	rt := &fakeRuntime{running: false}
	s := State{Desired: "sha256:same", Proven: "sha256:same"}
	sleeps := 0

	outcome, err := Reconcile(rt, s, "unused", noSleep(&sleeps))
	if err != nil || outcome != OutcomeOK {
		t.Fatalf("outcome=%v err=%v, expected OutcomeOK/nil", outcome, err)
	}
	if len(rt.startedFor) != 1 || rt.startedFor[0] != "sha256:same" {
		t.Fatalf("started %v, expected exactly one start of sha256:same", rt.startedFor)
	}
	if sleeps != 0 {
		t.Errorf("slept %d times, expected no await/rollback dance -- nothing to swap", sleeps)
	}
}

func TestReconcileSwapSucceeds(t *testing.T) {
	dir := t.TempDir()
	health := filepath.Join(dir, "health.env")
	rt := &fakeRuntime{running: false}
	rt.afterStart = func() {
		content := "timestamp=1000\ndigest=sha256:new\nversion=0.5.0\n"
		if err := os.WriteFile(health, []byte(content), 0o600); err != nil {
			t.Fatalf("setup failed: %v", err)
		}
	}
	s := State{Desired: "sha256:new", Proven: "sha256:old", Since: 500}

	outcome, err := Reconcile(rt, s, health, noSleep(new(int)))
	if err != nil || outcome != OutcomeOK {
		t.Fatalf("outcome=%v err=%v, expected OutcomeOK/nil", outcome, err)
	}
	if len(rt.startedFor) != 1 || rt.startedFor[0] != "sha256:new" {
		t.Fatalf("started %v, expected exactly one start of sha256:new, no rollback", rt.startedFor)
	}
}

func TestReconcileRollsBackWhenHealthNeverArrives(t *testing.T) {
	rt := &fakeRuntime{running: false}
	s := State{Desired: "sha256:new", Proven: "sha256:old", Since: 500}

	outcome, err := Reconcile(rt, s, filepath.Join(t.TempDir(), "missing.env"), noSleep(new(int)))
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
	s := State{Desired: "sha256:new", Proven: "sha256:old"}

	outcome, err := Reconcile(rt, s, filepath.Join(t.TempDir(), "missing.env"), noSleep(new(int)))
	if err != nil || outcome != OutcomeRolledBack {
		t.Fatalf("outcome=%v err=%v, expected OutcomeRolledBack/nil", outcome, err)
	}
}

func TestReconcileEmptyProvenReportsErrorWithoutAction(t *testing.T) {
	rt := &fakeRuntime{running: false}
	s := State{Desired: "sha256:new", Proven: ""}

	outcome, err := Reconcile(rt, s, filepath.Join(t.TempDir(), "missing.env"), noSleep(new(int)))
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

	outcome, err := Reconcile(rt, s, "unused", noSleep(new(int)))
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
