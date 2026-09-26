package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func newFakeLedDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for _, name := range []string{"brightness", "trigger", "delay_on", "delay_off"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("0\n"), 0o644); err != nil {
			t.Fatalf("preparing fake LED file %q: %v", name, err)
		}
	}
	return dir
}

// TestApplyOnceMissingLedDriverDoesNotError proves section 23.3's "keeps
// working unchanged without them" end to end through applyOnce: neither
// LED file exists (a mini PC, or the overlay not loaded), and nothing
// about applyOnce panics or writes anywhere.
func TestApplyOnceMissingLedDriverDoesNotError(t *testing.T) {
	missingDevice := filepath.Join(t.TempDir(), "brightness")
	missingSystem := filepath.Join(t.TempDir(), "brightness")

	applyOnce("", "", "", "", missingDevice, missingSystem, 360*time.Second, func() time.Time { return testNow })
	// No panic, and nothing was created.
	if _, err := os.Stat(missingDevice); err == nil {
		t.Fatal("expected no brightness file to have been created")
	}
}

// TestApplyOnceWritesBothLedsForAHealthyDevice exercises the full path
// (gatherInputs -> Decide* -> ledsysfs.ApplyPattern) for a healthy,
// registered device with an open fault -- LED 1 should end up steady on,
// LED 2 slow-blinking, both actually written to the fake sysfs files.
func TestApplyOnceWritesBothLedsForAHealthyDevice(t *testing.T) {
	dataDir := t.TempDir()
	stateFile := writeTestFile(t, dataDir, "state.env", "desired=sha256:aaaa\nsince=1000000000\n")
	healthFile := writeTestFile(t, dataDir, "health.env", "timestamp=1999999990\ndigest=sha256:aaaa\nversion=0.4.0\n")
	agentStatusFile := writeTestFile(t, dataDir, "agent-status.env", "timestamp=1999999990\ncloud_contact=ok\nfault=open\ncontrol=ok\n")

	deviceDir := newFakeLedDir(t)
	systemDir := newFakeLedDir(t)
	deviceBrightness := filepath.Join(deviceDir, "brightness")
	systemBrightness := filepath.Join(systemDir, "brightness")

	now := func() time.Time { return time.Unix(2000000000, 0) }
	applyOnce(stateFile, healthFile, "", agentStatusFile, deviceBrightness, systemBrightness, 360*time.Second, now)

	deviceTrigger, err := os.ReadFile(filepath.Join(deviceDir, "trigger"))
	if err != nil {
		t.Fatalf("reading device trigger: %v", err)
	}
	if string(deviceTrigger) != "none\n" {
		t.Errorf("device trigger = %q, want steady-on's \"none\\n\"", deviceTrigger)
	}
	deviceBrightnessContent, err := os.ReadFile(deviceBrightness)
	if err != nil {
		t.Fatalf("reading device brightness: %v", err)
	}
	if string(deviceBrightnessContent) != "1\n" {
		t.Errorf("device brightness = %q, want \"1\\n\" (steady on)", deviceBrightnessContent)
	}

	systemTrigger, err := os.ReadFile(filepath.Join(systemDir, "trigger"))
	if err != nil {
		t.Fatalf("reading system trigger: %v", err)
	}
	if string(systemTrigger) != "timer\n" {
		t.Errorf("system trigger = %q, want slow-blink's \"timer\\n\"", systemTrigger)
	}
}

// TestApplyOnceMissingInputFilesFallsBackToConservativePatterns: no state,
// health, registration, or agent status file exists yet (a device fresh
// out of the box, watchdog and agent never having run) -- both LEDs must
// still get a defined, conservative pattern, not an error or a panic.
func TestApplyOnceMissingInputFilesFallsBackToConservativePatterns(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "missing.env")

	deviceDir := newFakeLedDir(t)
	systemDir := newFakeLedDir(t)

	applyOnce(missing, missing, missing, missing, filepath.Join(deviceDir, "brightness"), filepath.Join(systemDir, "brightness"), 360*time.Second, func() time.Time { return testNow })

	deviceTrigger, _ := os.ReadFile(filepath.Join(deviceDir, "trigger"))
	if string(deviceTrigger) != "timer\n" {
		t.Errorf("device trigger = %q, want slow-blink's \"timer\\n\"", deviceTrigger)
	}
	systemTrigger, _ := os.ReadFile(filepath.Join(systemDir, "trigger"))
	if string(systemTrigger) != "timer\n" {
		t.Errorf("system trigger = %q, want slow-blink's \"timer\\n\"", systemTrigger)
	}
}

func TestRunCheckModePrintsUppercaseKeys(t *testing.T) {
	dir := t.TempDir()
	path := writeTestFile(t, dir, "agent-status.env", "timestamp=1790000200\ncloud_contact=lost\nfault=open\ncontrol=stalled\n")

	if code := runCheckMode(path); code != 0 {
		t.Fatalf("runCheckMode returned %d, want 0", code)
	}
}

func TestRunCheckModeMissingFileReturnsNonZero(t *testing.T) {
	if code := runCheckMode(filepath.Join(t.TempDir(), "missing.env")); code == 0 {
		t.Fatal("expected a non-zero exit code for a missing agent status file")
	}
}

func TestLedPresentEitherFalseWhenBothMissing(t *testing.T) {
	a := filepath.Join(t.TempDir(), "brightness")
	b := filepath.Join(t.TempDir(), "brightness")
	if ledPresentEither(a, b) {
		t.Fatal("expected false when neither LED file exists")
	}
}

func TestLedPresentEitherTrueWhenOnePresent(t *testing.T) {
	present := newFakeLedDir(t)
	missing := filepath.Join(t.TempDir(), "brightness")
	if !ledPresentEither(filepath.Join(present, "brightness"), missing) {
		t.Fatal("expected true when one LED file exists")
	}
}
