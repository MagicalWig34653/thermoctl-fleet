package main

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestIsSafeManifestPath(t *testing.T) {
	cases := []struct {
		path string
		want bool
	}{
		{"thermoctl.db", true},
		{"zigbee2mqtt/database.db", true},
		{"", false},
		{"/etc/passwd", false},
		{"..", false},
		{"../escape", false},
		{"a/../b", false},
		{"a/./b", false},
		{"a//b", false},
		{".", false},
		{"a/", false},
	}
	for _, c := range cases {
		if got := isSafeManifestPath(c.path); got != c.want {
			t.Errorf("isSafeManifestPath(%q) = %v, want %v", c.path, got, c.want)
		}
	}
}

func TestSameDeviceEqual(t *testing.T) {
	dir := t.TempDir()
	a, err := os.Stat(dir)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if !sameDevice(a, a) {
		t.Fatalf("sameDevice(a, a) = false, want true")
	}
}

func TestSameDeviceUnknownPlatform(t *testing.T) {
	// A FileInfo whose Sys() is not *syscall.Stat_t must fail closed
	// (never "same") rather than assume equality without proof.
	fake := fakeFileInfo{}
	if sameDevice(fake, fake) {
		t.Fatalf("sameDevice with no Stat_t = true, want false (fail closed)")
	}
}

type fakeFileInfo struct{ os.FileInfo }

func (fakeFileInfo) Sys() any { return nil }

func TestIsExdev(t *testing.T) {
	linkErr := &os.LinkError{Op: "rename", Err: syscall.EXDEV}
	if !isExdev(linkErr) {
		t.Fatalf("isExdev(EXDEV) = false, want true")
	}
	if isExdev(errors.New("some other error")) {
		t.Fatalf("isExdev(plain error) = true, want false")
	}
	other := &os.LinkError{Op: "rename", Err: syscall.ENOENT}
	if isExdev(other) {
		t.Fatalf("isExdev(ENOENT) = true, want false")
	}
}

func TestApplyDestinationOwnershipMatchesDestinationDir(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "moved.db")
	if err := os.WriteFile(target, []byte("x"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	dirInfo, err := os.Stat(dir)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	dirStat := dirInfo.Sys().(*syscall.Stat_t)

	applyDestinationOwnership(target, noopWarn)

	info, err := os.Stat(target)
	if err != nil {
		t.Fatalf("stat moved file: %v", err)
	}
	if info.Mode().Perm() != 0o644 {
		t.Fatalf("mode = %v, want 0644", info.Mode().Perm())
	}
	fileStat := info.Sys().(*syscall.Stat_t)
	if fileStat.Uid != dirStat.Uid || fileStat.Gid != dirStat.Gid {
		t.Fatalf("ownership %d:%d does not match destination dir %d:%d",
			fileStat.Uid, fileStat.Gid, dirStat.Uid, dirStat.Gid)
	}
}

func TestErrUnsafeAndDetailOf(t *testing.T) {
	err := errUnsafe(DetailHashMismatch)
	if err.Error() != DetailHashMismatch {
		t.Fatalf("Error() = %q, want %q", err.Error(), DetailHashMismatch)
	}
	if got := detailOf(err, DetailUnsafeStaging); got != DetailHashMismatch {
		t.Fatalf("detailOf(detailedError) = %q, want %q", got, DetailHashMismatch)
	}
	plain := errors.New("boom")
	if got := detailOf(plain, DetailUnsafeStaging); got != DetailUnsafeStaging {
		t.Fatalf("detailOf(plain error) = %q, want fallback %q", got, DetailUnsafeStaging)
	}
}

func TestWriteStatusFileMissingDirectoryFails(t *testing.T) {
	err := writeStatusFile(filepath.Join(t.TempDir(), "does-not-exist", "status.json"), Status{})
	if err == nil {
		t.Fatalf("expected an error writing into a non-existent directory")
	}
}

func TestParseManifestUnreadableFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ManifestFilename)
	if err := os.WriteFile(path, []byte("{}"), 0o000); err != nil {
		t.Fatalf("write: %v", err)
	}
	t.Cleanup(func() { os.Chmod(path, 0o600) })
	if os.Geteuid() == 0 {
		t.Skip("root ignores file permission bits")
	}
	_, present, err := parseManifest(path)
	if !present || err == nil {
		t.Fatalf("expected present=true, err!=nil for an unreadable manifest, got present=%v err=%v", present, err)
	}
}

func TestLstatIsDirNoSymlinkOnPlainFile(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "not-a-dir")
	if err := os.WriteFile(file, []byte("x"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	isDir, err := lstatIsDirNoSymlink(file)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if isDir {
		t.Fatalf("lstatIsDirNoSymlink on a plain file = true, want false")
	}
}
