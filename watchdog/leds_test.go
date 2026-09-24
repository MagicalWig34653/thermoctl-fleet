package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLedPresentMissing(t *testing.T) {
	path := filepath.Join(t.TempDir(), "brightness")
	if LedPresent(path) {
		t.Fatal("LedPresent reports true for a non-existent file")
	}
}

func TestLedPresentPresent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "brightness")
	if err := os.WriteFile(path, []byte("0"), 0o644); err != nil {
		t.Fatalf("creating test file failed: %v", err)
	}
	if !LedPresent(path) {
		t.Fatal("LedPresent reports false for an existing file")
	}
}

// TestLedSetPatternWithoutFileNoError checks the central point from
// section 23.3: a missing LED file is not an error, the watchdog keeps
// running unchanged.
func TestLedSetPatternWithoutFileNoError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "brightness")
	if err := LedSetPattern(path, "slow-blink"); err != nil {
		t.Fatalf("expected no error for a missing LED file, got: %v", err)
	}
}

func TestLedSetPatternNotImplemented(t *testing.T) {
	path := filepath.Join(t.TempDir(), "brightness")
	if err := os.WriteFile(path, []byte("0"), 0o644); err != nil {
		t.Fatalf("creating test file failed: %v", err)
	}
	if err := LedSetPattern(path, "steady-on"); err == nil {
		t.Fatal("expected error did not occur")
	}
}
