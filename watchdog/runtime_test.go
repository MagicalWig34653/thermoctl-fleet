package main

import (
	"os/exec"
	"strings"
	"testing"
)

// swapExecCommand records every argv execCommand is called with (as
// []string{name, args...}) and returns a harmless local command instead of
// ever touching Docker -- cross-review R3: the built argv is what gets
// tested, not a real container runtime. Restored automatically.
func swapExecCommand(t *testing.T, calls *[][]string) {
	t.Helper()
	original := execCommand
	t.Cleanup(func() { execCommand = original })
	execCommand = func(name string, args ...string) *exec.Cmd {
		*calls = append(*calls, append([]string{name}, args...))
		return exec.Command("true")
	}
}

func TestCliRuntimeStartTagsRepoAtDigestThenComposesWithPullNever(t *testing.T) {
	var calls [][]string
	swapExecCommand(t, &calls)
	r := cliRuntime{bin: "docker", compose: "/etc/thermoctl-agent/compose.yml", repo: "ghcr.io/x/thermoctl-agent"}
	digest := "sha256:" + strings.Repeat("a", 64)

	if err := r.Start(digest); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(calls) != 2 {
		t.Fatalf("got %d exec calls, expected exactly 2 (tag, compose up)", len(calls))
	}
	want := []string{"docker", "tag", "ghcr.io/x/thermoctl-agent@" + digest, "thermoctl-agent:current"}
	if !equalArgs(calls[0], want) {
		t.Errorf("tag call = %v, expected %v", calls[0], want)
	}
	up := strings.Join(calls[1], " ")
	if !strings.Contains(up, " --pull never ") {
		t.Errorf("compose up call = %q, expected --pull never present", up)
	}
	if !strings.Contains(up, "/etc/thermoctl-agent/compose.yml") {
		t.Errorf("compose up call = %q, expected the configured compose path", up)
	}
}

func TestCliRuntimeStartRefusesAnythingNotAPlainDigest(t *testing.T) {
	var calls [][]string
	swapExecCommand(t, &calls)
	r := cliRuntime{bin: "docker", compose: "/etc/thermoctl-agent/compose.yml", repo: "ghcr.io/x/thermoctl-agent"}

	for _, bad := range []string{
		"latest",
		"-rm",                               // cross-review R4: must never reach argv as an option
		"sha256:" + strings.Repeat("a", 63), // too short
		"sha256:" + strings.Repeat("g", 64), // not hex
		"ghcr.io/x/thermoctl-agent@sha256:" + strings.Repeat("a", 64), // repo prefix does not belong in Start's argument -- Start adds it
	} {
		if err := r.Start(bad); err == nil {
			t.Errorf("digest %q: expected error did not occur", bad)
		}
	}
	if len(calls) != 0 {
		t.Fatalf("exec called with %v, expected no command for any refused digest", calls)
	}
}

// fakeInspect returns an execCommand that answers the container inspect
// ("true"/"false" <restarts> <imageID>) and the image inspect
// ("{{range .RepoDigests}}...") calls Status makes, distinguished by
// whether "image" is the first argument -- exactly how cliRuntime calls
// them, never a real Docker.
func fakeInspect(containerLine, repoDigestsLine string) func(string, ...string) *exec.Cmd {
	return func(_ string, args ...string) *exec.Cmd {
		if len(args) > 0 && args[0] == "image" {
			return exec.Command("echo", strings.Fields(repoDigestsLine)...)
		}
		return exec.Command("echo", strings.Fields(containerLine)...)
	}
}

func TestCliRuntimeStatusResolvesManifestDigestFromRepoDigests(t *testing.T) {
	original := execCommand
	t.Cleanup(func() { execCommand = original })
	digest := "sha256:" + strings.Repeat("a", 64)
	imageID := "sha256:" + strings.Repeat("b", 64)
	execCommand = fakeInspect("true 2 "+imageID, "other-repo@sha256:"+strings.Repeat("c", 64)+" myrepo@"+digest)

	r := cliRuntime{repo: "myrepo"}
	running, gotDigest, restarts, err := r.Status()
	if err != nil || !running || restarts != 2 || gotDigest != digest {
		t.Fatalf("got (%v,%q,%d,%v), expected (true,%q,2,nil)", running, gotDigest, restarts, err, digest)
	}
}

func TestCliRuntimeStatusUnmatchedRepoDigestReportsEmpty(t *testing.T) {
	// An image pulled by tag, or from a different source entirely, must not
	// be mistaken for a match -- Reconcile then treats it as "something
	// other than desired is running" (cross-review, main session finding).
	original := execCommand
	t.Cleanup(func() { execCommand = original })
	imageID := "sha256:" + strings.Repeat("b", 64)
	execCommand = fakeInspect("true 1 "+imageID, "other-repo@sha256:"+strings.Repeat("c", 64))

	r := cliRuntime{repo: "myrepo"}
	running, digest, restarts, err := r.Status()
	if err != nil || !running || restarts != 1 || digest != "" {
		t.Fatalf("got (%v,%q,%d,%v), expected running with an empty (unmatched) digest", running, digest, restarts, err)
	}
}

func TestCliRuntimeStatusReportsNotRunningOnExecFailure(t *testing.T) {
	// No container of that name yet (first boot, or right after Start's own
	// tag step) is "not running", not an error.
	original := execCommand
	t.Cleanup(func() { execCommand = original })
	execCommand = func(string, ...string) *exec.Cmd { return exec.Command("false") }

	running, digest, restarts, err := cliRuntime{}.Status()
	if err != nil || running || digest != "" || restarts != 0 {
		t.Fatalf("got (%v,%q,%d,%v), expected the not-running zero value", running, digest, restarts, err)
	}
}

func equalArgs(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}
