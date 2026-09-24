package main

import "testing"

// These tests only confirm that every scaffold function actually fails
// instead of pretending something -- none confirms an already-present
// capability (there is none here yet).

func TestAgentStoppedNotImplemented(t *testing.T) {
	if _, err := AgentStopped(); err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestStartDigestNotImplemented(t *testing.T) {
	if err := StartDigest(State{Desired: "sha256:9f2c"}); err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestAwaitHealthReportNotImplemented(t *testing.T) {
	if _, err := AwaitHealthReport(); err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestRollBackToProvenWithoutProvenState(t *testing.T) {
	if err := RollBackToProven(State{Desired: "sha256:9f2c"}); err == nil {
		t.Fatal("expected error did not occur")
	}
}

func TestRollBackToProvenNotImplemented(t *testing.T) {
	s := State{Desired: "sha256:9f2c", Proven: "sha256:1a7b"}
	if err := RollBackToProven(s); err == nil {
		t.Fatal("expected error did not occur")
	}
}
