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

func TestOpenStagedFileNoFollowRefusesHardLinkedFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "thermoctl.db")
	writeFile(t, path, []byte("x"))
	link := filepath.Join(dir, "another-name-for-the-same-inode")
	if err := os.Link(path, link); err != nil {
		t.Skipf("hard links not supported on this filesystem: %v", err)
	}

	_, _, err := openStagedFileNoFollow(path)
	if err != errHardLinked {
		t.Fatalf("err = %v, want errHardLinked", err)
	}
}

func TestOpenStagedFileNoFollowAcceptsASingleLinkFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "thermoctl.db")
	writeFile(t, path, []byte("hello"))

	file, size, err := openStagedFileNoFollow(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer file.Close()
	if size != 5 {
		t.Fatalf("size = %d, want 5", size)
	}
}

func TestOpenStagedFileNoFollowRefusesSymlink(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "target.db")
	writeFile(t, target, []byte("x"))
	link := filepath.Join(dir, "thermoctl.db")
	if err := os.Symlink(target, link); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	_, _, err := openStagedFileNoFollow(link)
	if err != errNotRegular {
		t.Fatalf("err = %v, want errNotRegular", err)
	}
}

func TestCreateTempInDirProducesAUniqueNoFollowFile(t *testing.T) {
	dir := t.TempDir()
	file, path, err := createTempInDir(dir)
	if err != nil {
		t.Fatalf("createTempInDir: %v", err)
	}
	defer file.Close()
	defer os.Remove(path)

	if filepath.Dir(path) != dir {
		t.Fatalf("temp file created in %q, want %q", filepath.Dir(path), dir)
	}
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatalf("lstat: %v", err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		t.Fatalf("temp file is not a plain regular file: %v", info.Mode())
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("temp file mode = %v, want 0600", info.Mode().Perm())
	}

	// A second call must produce a different, non-colliding name.
	file2, path2, err := createTempInDir(dir)
	if err != nil {
		t.Fatalf("createTempInDir (second): %v", err)
	}
	defer file2.Close()
	defer os.Remove(path2)
	if path == path2 {
		t.Fatalf("two calls to createTempInDir produced the same path: %q", path)
	}
}

func TestApplyDestinationOwnershipMatchesDestinationDir(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "moved.db")
	file, err := os.Create(target)
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	defer file.Close()

	dirInfo, err := os.Lstat(dir)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	dirStat := dirInfo.Sys().(*syscall.Stat_t)

	if err := applyDestinationOwnership(file, dir); err != nil {
		t.Fatalf("applyDestinationOwnership: %v", err)
	}

	info, err := file.Stat()
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
	_, _, present, err := parseManifest(path)
	if !present || err == nil {
		t.Fatalf("expected present=true, err!=nil for an unreadable manifest, got present=%v err=%v", present, err)
	}
}

func TestParseManifestRefusesASymlink(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "elsewhere.json")
	writeFile(t, target, []byte("{}"))
	link := filepath.Join(dir, ManifestFilename)
	if err := os.Symlink(target, link); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	_, _, present, err := parseManifest(link)
	if !present || err == nil {
		t.Fatalf("expected present=true, err!=nil for a symlinked manifest, got present=%v err=%v", present, err)
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

func TestCleanupPreparedRemovesTempFiles(t *testing.T) {
	dir := t.TempDir()
	var prepared []preparedFile
	for i := 0; i < 3; i++ {
		file, path, err := createTempInDir(dir)
		if err != nil {
			t.Fatalf("createTempInDir: %v", err)
		}
		file.Close()
		prepared = append(prepared, preparedFile{TempPath: path})
	}

	cleanupPrepared(prepared)

	for _, one := range prepared {
		if _, err := os.Stat(one.TempPath); !os.IsNotExist(err) {
			t.Fatalf("temp file %s was not removed by cleanupPrepared", one.TempPath)
		}
	}
}
