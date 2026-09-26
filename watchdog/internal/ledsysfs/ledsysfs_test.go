package ledsysfs

import (
	"os"
	"path/filepath"
	"testing"
)

// newFakeLedDir creates a temp directory standing in for a kernel LED
// class directory (section 23.1) with all four attribute files a real one
// exposes once created -- unlike the real kernel, a temp dir does not grow
// delay_on/delay_off dynamically when "timer" is written to trigger, so
// they are pre-created here to let ApplyPattern's writes succeed.
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

func readFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %q: %v", path, err)
	}
	return string(data)
}

func TestLedPresentMissing(t *testing.T) {
	path := filepath.Join(t.TempDir(), "brightness")
	if LedPresent(path) {
		t.Fatal("LedPresent reports true for a non-existent file")
	}
}

func TestLedPresentPresent(t *testing.T) {
	dir := newFakeLedDir(t)
	if !LedPresent(filepath.Join(dir, "brightness")) {
		t.Fatal("LedPresent reports false for an existing file")
	}
}

// TestApplyPatternWithoutFileNoError is the central point from section
// 23.3: a missing LED file is not an error, the program keeps running
// unchanged (and must not spam its log every poll either -- checked at the
// cmd/thermoctl-leds level, not here).
func TestApplyPatternWithoutFileNoError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "brightness")
	if err := ApplyPattern(path, SlowBlink); err != nil {
		t.Fatalf("expected no error for a missing LED file, got: %v", err)
	}
}

func TestApplyPatternUnknownPatternIsAnError(t *testing.T) {
	dir := newFakeLedDir(t)
	if err := ApplyPattern(filepath.Join(dir, "brightness"), Pattern("not-a-real-pattern")); err == nil {
		t.Fatal("expected an error for an unknown pattern")
	}
}

func TestApplyPatternOffWritesTriggerNoneAndZeroBrightness(t *testing.T) {
	dir := newFakeLedDir(t)
	brightness := filepath.Join(dir, "brightness")

	if err := ApplyPattern(brightness, Off); err != nil {
		t.Fatalf("ApplyPattern: %v", err)
	}
	if got := readFile(t, filepath.Join(dir, "trigger")); got != "none\n" {
		t.Errorf("trigger = %q, want \"none\\n\"", got)
	}
	if got := readFile(t, brightness); got != "0\n" {
		t.Errorf("brightness = %q, want \"0\\n\"", got)
	}
}

func TestApplyPatternSteadyOnWritesTriggerNoneAndBrightnessOne(t *testing.T) {
	dir := newFakeLedDir(t)
	brightness := filepath.Join(dir, "brightness")

	if err := ApplyPattern(brightness, SteadyOn); err != nil {
		t.Fatalf("ApplyPattern: %v", err)
	}
	if got := readFile(t, filepath.Join(dir, "trigger")); got != "none\n" {
		t.Errorf("trigger = %q, want \"none\\n\"", got)
	}
	if got := readFile(t, brightness); got != "1\n" {
		t.Errorf("brightness = %q, want \"1\\n\"", got)
	}
}

// TestApplyPatternTimerPatternsWriteDistinctPeriods proves the three
// timer-driven patterns (slow blink, fast blink, "two short blinks") each
// select the "timer" trigger, and that all three periods are pairwise
// distinct -- otherwise two of section 23.2's meanings would look
// identical on the actual hardware.
func TestApplyPatternTimerPatternsWriteDistinctPeriods(t *testing.T) {
	cases := []struct {
		name    string
		pattern Pattern
	}{
		{"slow-blink", SlowBlink},
		{"fast-blink", FastBlink},
		{"two-short-blinks", TwoShortBlinks},
	}
	seen := map[string]bool{}
	for _, c := range cases {
		dir := newFakeLedDir(t)
		brightness := filepath.Join(dir, "brightness")
		if err := ApplyPattern(brightness, c.pattern); err != nil {
			t.Fatalf("%s: ApplyPattern: %v", c.name, err)
		}
		if got := readFile(t, filepath.Join(dir, "trigger")); got != "timer\n" {
			t.Errorf("%s: trigger = %q, want \"timer\\n\"", c.name, got)
		}
		onOff := readFile(t, filepath.Join(dir, "delay_on")) + "/" + readFile(t, filepath.Join(dir, "delay_off"))
		if seen[onOff] {
			t.Errorf("%s: delay_on/delay_off pair %q collides with an earlier pattern", c.name, onOff)
		}
		seen[onOff] = true
	}
}
