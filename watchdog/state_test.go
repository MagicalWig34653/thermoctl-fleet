package main

import (
	"strings"
	"testing"
)

func TestParseStateComplete(t *testing.T) {
	input := "desired=sha256:9f2c\nproven=sha256:1a7b\nsince=1790000123\n"

	s, err := ParseState(strings.NewReader(input))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if s.Desired != "sha256:9f2c" {
		t.Errorf("Desired = %q, expected sha256:9f2c", s.Desired)
	}
	if s.Proven != "sha256:1a7b" {
		t.Errorf("Proven = %q, expected sha256:1a7b", s.Proven)
	}
	if s.Since != 1790000123 {
		t.Errorf("Since = %d, expected 1790000123", s.Since)
	}
}

func TestParseStateWithoutProven(t *testing.T) {
	// A freshly set-up device has no proven revision yet (see
	// docs/STATUS.md) -- the line is entirely missing here.
	input := "desired=sha256:9f2c\nsince=1790000123\n"

	s, err := ParseState(strings.NewReader(input))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if s.Proven != "" {
		t.Errorf("Proven = %q, expected empty", s.Proven)
	}
}

func TestParseStateWithoutDesiredIsRejected(t *testing.T) {
	input := "proven=sha256:1a7b\nsince=1790000123\n"

	_, err := ParseState(strings.NewReader(input))
	if err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestParseStateRejectsLineWithoutEqualsSign(t *testing.T) {
	input := "desired=sha256:9f2c\nsomething without an equals sign\n"

	_, err := ParseState(strings.NewReader(input))
	if err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestParseStateRejectsInvalidTimestamp(t *testing.T) {
	input := "desired=sha256:9f2c\nsince=not-a-number\n"

	_, err := ParseState(strings.NewReader(input))
	if err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestParseStateIgnoresCommentsAndUnknownKeys(t *testing.T) {
	// Section 18.2, applied analogously: a new field must not keep the
	// watchdog from reading the known fields.
	input := "# comment\ndesired=sha256:9f2c\nfuture_field=something\n"

	s, err := ParseState(strings.NewReader(input))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if s.Desired != "sha256:9f2c" {
		t.Errorf("Desired = %q, expected sha256:9f2c", s.Desired)
	}
}

func TestLoadStateMissingFile(t *testing.T) {
	_, err := LoadState("/path/that/does/not/exist.env")
	if err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestParseStateReadsEsimFallbackLines(t *testing.T) {
	// Section 24.4, decided afterward: the fallback clock for an eSIM
	// profile switch is two further lines in the same file.
	input := "desired=sha256:9f2c\nesim_previous_profile=profile-1\nesim_deadline=1790000723\n"

	s, err := ParseState(strings.NewReader(input))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if s.EsimPreviousProfile != "profile-1" {
		t.Errorf("EsimPreviousProfile = %q, expected profile-1", s.EsimPreviousProfile)
	}
	if s.EsimDeadline != 1790000723 {
		t.Errorf("EsimDeadline = %d, expected 1790000723", s.EsimDeadline)
	}
}

func TestParseStateWithoutEsimLinesStaysEmpty(t *testing.T) {
	// No profile switch pending -- neither of the two lines present.
	input := "desired=sha256:9f2c\n"

	s, err := ParseState(strings.NewReader(input))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if s.EsimPreviousProfile != "" || s.EsimDeadline != 0 {
		t.Errorf("esim fields = %q/%d, expected empty/0", s.EsimPreviousProfile, s.EsimDeadline)
	}
}

func TestParseStateRejectsInvalidEsimDeadline(t *testing.T) {
	input := "desired=sha256:9f2c\nesim_deadline=not-a-number\n"

	_, err := ParseState(strings.NewReader(input))
	if err == nil {
		t.Fatal("expected error did not occur")
	}
}
