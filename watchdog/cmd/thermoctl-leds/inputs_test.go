package main

import (
	"os"
	"path/filepath"
	"testing"
)

func writeTestFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("writing %q: %v", path, err)
	}
	return path
}

func TestLoadWatchdogStateParsesDesiredAndSince(t *testing.T) {
	dir := t.TempDir()
	path := writeTestFile(t, dir, "state.env", "desired=sha256:aaaa\nproven=sha256:bbbb\nsince=1790000000\n")

	state, err := loadWatchdogState(path)
	if err != nil {
		t.Fatalf("loadWatchdogState: %v", err)
	}
	if state == nil {
		t.Fatal("expected a non-nil state")
	}
	if state.Desired != "sha256:aaaa" || state.Since != 1790000000 {
		t.Errorf("got %+v", state)
	}
}

func TestLoadWatchdogStateMissingFileIsNilNotError(t *testing.T) {
	state, err := loadWatchdogState(filepath.Join(t.TempDir(), "does-not-exist.env"))
	if err != nil {
		t.Fatalf("expected no error for a missing file, got: %v", err)
	}
	if state != nil {
		t.Fatalf("expected nil state for a missing file, got %+v", state)
	}
}

func TestLoadHealthReportParsesTimestampAndDigest(t *testing.T) {
	dir := t.TempDir()
	path := writeTestFile(t, dir, "health.env", "timestamp=1790000100\ndigest=sha256:cccc\nversion=0.4.0\n")

	health, err := loadHealthReport(path)
	if err != nil {
		t.Fatalf("loadHealthReport: %v", err)
	}
	if health == nil || health.Timestamp != 1790000100 || health.Digest != "sha256:cccc" {
		t.Errorf("got %+v", health)
	}
}

func TestLoadRegistrationStatusParsesStatus(t *testing.T) {
	dir := t.TempDir()
	path := writeTestFile(t, dir, "registration_status", "status=waiting_for_assignment\nverification_code=1234\n")

	status, err := loadRegistrationStatus(path)
	if err != nil {
		t.Fatalf("loadRegistrationStatus: %v", err)
	}
	if status == nil || status.Status != "waiting_for_assignment" {
		t.Errorf("got %+v", status)
	}
}

func TestLoadAgentStatusParsesAllFourFields(t *testing.T) {
	dir := t.TempDir()
	path := writeTestFile(t, dir, "agent-status.env", "timestamp=1790000200\ncloud_contact=lost\nfault=open\ncontrol=stalled\n")

	status, err := loadAgentStatus(path)
	if err != nil {
		t.Fatalf("loadAgentStatus: %v", err)
	}
	if status == nil {
		t.Fatal("expected a non-nil status")
	}
	if status.Timestamp != 1790000200 || status.CloudContact != "lost" || status.Fault != "open" || status.Control != "stalled" {
		t.Errorf("got %+v", status)
	}
}

func TestLoadAgentStatusMissingFileIsNilNotError(t *testing.T) {
	status, err := loadAgentStatus(filepath.Join(t.TempDir(), "does-not-exist.env"))
	if err != nil {
		t.Fatalf("expected no error for a missing file, got: %v", err)
	}
	if status != nil {
		t.Fatalf("expected nil status for a missing file, got %+v", status)
	}
}

func TestLoadAgentStatusMissingTimestampIsAnError(t *testing.T) {
	dir := t.TempDir()
	path := writeTestFile(t, dir, "agent-status.env", "cloud_contact=ok\nfault=none\ncontrol=ok\n")

	if _, err := loadAgentStatus(path); err == nil {
		t.Fatal("expected an error for a missing timestamp field")
	}
}

func TestLoadAgentStatusUnknownKeyIsIgnored(t *testing.T) {
	// section 18.2's extensibility rule, applied analogously: a future,
	// still-unknown key must not keep this program from reading the keys
	// it does know.
	dir := t.TempDir()
	path := writeTestFile(t, dir, "agent-status.env", "timestamp=1790000200\ncloud_contact=ok\nfault=none\ncontrol=ok\nfuture_field=surprise\n")

	status, err := loadAgentStatus(path)
	if err != nil {
		t.Fatalf("loadAgentStatus: %v", err)
	}
	if status.CloudContact != "ok" {
		t.Errorf("got %+v", status)
	}
}

func TestLoadAgentStatusLineWithoutEqualsIsAnError(t *testing.T) {
	dir := t.TempDir()
	path := writeTestFile(t, dir, "agent-status.env", "not a key value line\n")

	if _, err := loadAgentStatus(path); err == nil {
		t.Fatal("expected an error for a line without '='")
	}
}
