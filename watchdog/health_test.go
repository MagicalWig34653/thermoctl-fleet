package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReadHealthComplete(t *testing.T) {
	path := filepath.Join(t.TempDir(), "health")
	content := "timestamp=1790000123\ndigest=sha256:9f2c\nversion=0.4.0\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("setup failed: %v", err)
	}

	h, err := ReadHealth(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if h.Timestamp != 1790000123 {
		t.Errorf("Timestamp = %d, expected 1790000123", h.Timestamp)
	}
	if h.Digest != "sha256:9f2c" {
		t.Errorf("Digest = %q, expected sha256:9f2c", h.Digest)
	}
	if h.Version != "0.4.0" {
		t.Errorf("Version = %q, expected 0.4.0", h.Version)
	}
}

func TestParseHealthRejectsInvalidTimestamp(t *testing.T) {
	_, err := ParseHealth(strings.NewReader("timestamp=not-a-timestamp\n"))
	if err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestParseHealthRejectsMissingTimestamp(t *testing.T) {
	_, err := ParseHealth(strings.NewReader("digest=sha256:9f2c\nversion=0.4.0\n"))
	if err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestParseHealthRejectsLineWithoutEqualsSign(t *testing.T) {
	_, err := ParseHealth(strings.NewReader("timestamp=1790000123\nsomething without an equals sign\n"))
	if err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestParseHealthIgnoresUnknownKeys(t *testing.T) {
	// As with the state file: a future field must not prevent reading the
	// known fields.
	h, err := ParseHealth(strings.NewReader("timestamp=1790000123\nfuture_field=something\n"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if h.Timestamp != 1790000123 {
		t.Errorf("Timestamp = %d, expected 1790000123", h.Timestamp)
	}
}

func TestReadHealthMissingFile(t *testing.T) {
	_, err := ReadHealth(filepath.Join(t.TempDir(), "missing"))
	if err == nil {
		t.Fatal("expected error did not occur")
	}
}
