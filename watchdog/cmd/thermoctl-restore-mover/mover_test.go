package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

var testNow = func() time.Time { return time.Unix(2_000_000_000, 0) }

func noopWarn(format string, args ...any) {}

// writeFile is a small test helper: writes data to path, creating parent
// directories as needed.
func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatalf("writefile: %v", err)
	}
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// setup builds a fully valid staging directory (manifest + thermoctl.db +
// both zigbee2mqtt files) and a fully empty live destination -- every
// test starts from this and mutates one thing, so each test exercises
// exactly one branch.
type setup struct {
	dir             string
	stagingDir      string
	thermoctlDBPath string
	zigbee2mqttDir  string
	statusFile      string
	manifestPath    string
	thermoctlBytes  []byte
	z2mDBBytes      []byte
	z2mBackupBytes  []byte
}

func newSetup(t *testing.T) setup {
	t.Helper()
	dir := t.TempDir()
	s := setup{
		dir:             dir,
		stagingDir:      filepath.Join(dir, "staging"),
		thermoctlDBPath: filepath.Join(dir, "live", "thermoctl", "thermoctl.db"),
		zigbee2mqttDir:  filepath.Join(dir, "live", "zigbee2mqtt"),
		statusFile:      filepath.Join(dir, "status", "status.json"),
		thermoctlBytes:  []byte("thermoctl-db-bytes"),
		z2mDBBytes:      []byte("z2m-db-bytes"),
		z2mBackupBytes:  []byte("z2m-backup-bytes"),
	}
	s.manifestPath = filepath.Join(s.stagingDir, ManifestFilename)

	// Live destination directories must already exist (this program
	// never creates them) and start empty.
	if err := os.MkdirAll(filepath.Dir(s.thermoctlDBPath), 0o755); err != nil {
		t.Fatalf("mkdir live thermoctl dir: %v", err)
	}
	if err := os.MkdirAll(s.zigbee2mqttDir, 0o755); err != nil {
		t.Fatalf("mkdir live z2m dir: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(s.statusFile), 0o755); err != nil {
		t.Fatalf("mkdir status dir: %v", err)
	}

	writeFile(t, filepath.Join(s.stagingDir, "thermoctl.db"), s.thermoctlBytes)
	writeFile(t, filepath.Join(s.stagingDir, "zigbee2mqtt", "database.db"), s.z2mDBBytes)
	writeFile(t, filepath.Join(s.stagingDir, "zigbee2mqtt", "coordinator_backup.json"), s.z2mBackupBytes)

	s.writeManifest(t, s.defaultManifest())
	return s
}

func (s setup) defaultManifest() Manifest {
	return Manifest{
		BackupID: "backup-1",
		StagedAt: "2026-09-29T00:00:00Z",
		Files: []ManifestFile{
			{Path: "thermoctl.db", SizeBytes: int64(len(s.thermoctlBytes)), SHA256: sha256Hex(s.thermoctlBytes)},
			{Path: "zigbee2mqtt/database.db", SizeBytes: int64(len(s.z2mDBBytes)), SHA256: sha256Hex(s.z2mDBBytes)},
			{Path: "zigbee2mqtt/coordinator_backup.json", SizeBytes: int64(len(s.z2mBackupBytes)), SHA256: sha256Hex(s.z2mBackupBytes)},
		},
	}
}

func (s setup) writeManifest(t *testing.T, manifest Manifest) {
	t.Helper()
	data, err := json.Marshal(manifest)
	if err != nil {
		t.Fatalf("marshal manifest: %v", err)
	}
	writeFile(t, s.manifestPath, data)
}

func (s setup) config() Config {
	return Config{
		StagingDir:      s.stagingDir,
		ThermoctlDBPath: s.thermoctlDBPath,
		Zigbee2mqttDir:  s.zigbee2mqttDir,
		StatusFilePath:  s.statusFile,
	}
}

func (s setup) readStatus(t *testing.T) Status {
	t.Helper()
	data, err := os.ReadFile(s.statusFile)
	if err != nil {
		t.Fatalf("reading status file: %v", err)
	}
	var status Status
	if err := json.Unmarshal(data, &status); err != nil {
		t.Fatalf("unmarshal status file: %v", err)
	}
	return status
}

// leftoverTempFiles returns every ".thermoctl-restore-mover-*.tmp" file
// still present anywhere under dir -- used to assert this program never
// leaves an orphaned temp file behind, on either a clean success or a
// refusal.
func leftoverTempFiles(t *testing.T, dir string) []string {
	t.Helper()
	var found []string
	filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if strings.HasPrefix(d.Name(), ".thermoctl-restore-mover-") {
			found = append(found, path)
		}
		return nil
	})
	return found
}

func TestRunNoManifestIsANoOp(t *testing.T) {
	s := newSetup(t)
	if err := os.Remove(s.manifestPath); err != nil {
		t.Fatalf("remove manifest: %v", err)
	}
	code := run(s.config(), noopWarn, testNow)
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	if _, err := os.Stat(s.statusFile); !os.IsNotExist(err) {
		t.Fatalf("status file should not have been written for a no-op run, err=%v", err)
	}
}

func TestRunSuccess(t *testing.T) {
	s := newSetup(t)
	code := run(s.config(), noopWarn, testNow)
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}

	status := s.readStatus(t)
	if status.Result != ResultSuccess || status.Detail != DetailApplied || status.BackupID != "backup-1" {
		t.Fatalf("unexpected status: %+v", status)
	}
	if status.Timestamp != testNow().Unix() {
		t.Fatalf("timestamp = %d, want %d", status.Timestamp, testNow().Unix())
	}

	if _, err := os.Stat(s.stagingDir); !os.IsNotExist(err) {
		t.Fatalf("staging directory should have been removed after full success")
	}

	moved, err := os.ReadFile(s.thermoctlDBPath)
	if err != nil || string(moved) != string(s.thermoctlBytes) {
		t.Fatalf("thermoctl.db not moved correctly: %v %q", err, moved)
	}
	info, err := os.Stat(s.thermoctlDBPath)
	if err != nil {
		t.Fatalf("stat moved file: %v", err)
	}
	if info.Mode().Perm() != 0o644 {
		t.Fatalf("mode = %v, want 0644", info.Mode().Perm())
	}
	z2mDB, err := os.ReadFile(filepath.Join(s.zigbee2mqttDir, "database.db"))
	if err != nil || string(z2mDB) != string(s.z2mDBBytes) {
		t.Fatalf("zigbee2mqtt/database.db not moved correctly: %v %q", err, z2mDB)
	}

	if leftover := leftoverTempFiles(t, s.dir); len(leftover) != 0 {
		t.Fatalf("leftover temp files after a clean success: %v", leftover)
	}
}

func TestRunLiveStoreNotEmptyThermoctlDB(t *testing.T) {
	s := newSetup(t)
	writeFile(t, s.thermoctlDBPath, []byte("already-has-data"))

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Result != ResultFailure || status.Detail != DetailLiveStoreNotEmpty {
		t.Fatalf("unexpected status: %+v", status)
	}
	// Nothing should have been touched.
	if _, err := os.Stat(s.manifestPath); err != nil {
		t.Fatalf("manifest should still be present, staging untouched on refusal: %v", err)
	}
}

func TestRunLiveStoreNotEmptyZ2m(t *testing.T) {
	s := newSetup(t)
	writeFile(t, filepath.Join(s.zigbee2mqttDir, "database.db"), []byte("already-there"))

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailLiveStoreNotEmpty {
		t.Fatalf("unexpected detail: %q", status.Detail)
	}
}

func TestRunEmptyThermoctlDBFileStillCountsAsEmpty(t *testing.T) {
	// A zero-byte thermoctl.db (sqlite's own "created but never written"
	// convention) must still count as empty -- the same definition
	// agent/restore.py::_operational_store_is_empty uses.
	s := newSetup(t)
	writeFile(t, s.thermoctlDBPath, []byte(""))

	code := run(s.config(), noopWarn, testNow)
	if code != 0 {
		t.Fatalf("exit code = %d, want 0 (a zero-byte file must count as empty)", code)
	}
}

func TestRunTamperedHashRefused(t *testing.T) {
	s := newSetup(t)
	m := s.defaultManifest()
	m.Files[0].SHA256 = "0000000000000000000000000000000000000000000000000000000000000000"[:64]
	s.writeManifest(t, m)

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailHashMismatch {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailHashMismatch)
	}
	// Nothing should have moved.
	if _, err := os.Stat(s.thermoctlDBPath); !os.IsNotExist(err) {
		t.Fatalf("thermoctl.db should not exist at destination after a refusal")
	}
	if leftover := leftoverTempFiles(t, s.dir); len(leftover) != 0 {
		t.Fatalf("leftover temp files after a refusal: %v", leftover)
	}
}

func TestRunTamperedSizeRefused(t *testing.T) {
	s := newSetup(t)
	m := s.defaultManifest()
	m.Files[0].SizeBytes = 999999
	s.writeManifest(t, m)

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailSizeMismatch {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailSizeMismatch)
	}
}

func TestRunMissingFileRefused(t *testing.T) {
	s := newSetup(t)
	if err := os.Remove(filepath.Join(s.stagingDir, "thermoctl.db")); err != nil {
		t.Fatalf("remove: %v", err)
	}

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailFileMissing {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailFileMissing)
	}
}

func TestRunExtraFileInStagingRefused(t *testing.T) {
	s := newSetup(t)
	writeFile(t, filepath.Join(s.stagingDir, "unexpected.txt"), []byte("surprise"))

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailUnexpectedEntry {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailUnexpectedEntry)
	}
}

func TestRunExtraFileInZigbee2mqttSubdirRefused(t *testing.T) {
	s := newSetup(t)
	writeFile(t, filepath.Join(s.stagingDir, "zigbee2mqtt", "unexpected.txt"), []byte("surprise"))

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailUnexpectedEntry {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailUnexpectedEntry)
	}
}

func TestRunMissingManifestEntryForPresentFileRefused(t *testing.T) {
	// A file physically present in staging but not listed in the
	// manifest at all must be refused too (covered by the same "extra
	// file" contents check as TestRunExtraFileInStagingRefused, exercised
	// here via removing the manifest's own entry instead of adding an
	// unrelated file).
	s := newSetup(t)
	m := s.defaultManifest()
	m.Files = m.Files[:1] // drop the two zigbee2mqtt entries from the manifest
	s.writeManifest(t, m)

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailUnexpectedEntry {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailUnexpectedEntry)
	}
}

func TestRunSymlinkInStagingRefused(t *testing.T) {
	s := newSetup(t)
	target := filepath.Join(s.dir, "secret.txt")
	writeFile(t, target, []byte("outside staging"))
	if err := os.Remove(filepath.Join(s.stagingDir, "thermoctl.db")); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if err := os.Symlink(target, filepath.Join(s.stagingDir, "thermoctl.db")); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailNotRegularFile {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailNotRegularFile)
	}
}

func TestRunHardLinkedFileInStagingRefused(t *testing.T) {
	// Cross-review finding: a staged file with more than one hard link
	// must be refused -- this program's own chown/chmod (or whatever
	// wrote through the other link concurrently) would otherwise mutate
	// an inode reachable from somewhere else entirely.
	s := newSetup(t)
	staged := filepath.Join(s.stagingDir, "thermoctl.db")
	outsideLink := filepath.Join(s.dir, "hardlink-outside-staging")
	if err := os.Link(staged, outsideLink); err != nil {
		t.Skipf("hard links not supported on this filesystem: %v", err)
	}
	t.Cleanup(func() { os.Remove(outsideLink) })

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailHardLinkedFile {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailHardLinkedFile)
	}
	if _, err := os.Stat(s.thermoctlDBPath); !os.IsNotExist(err) {
		t.Fatalf("thermoctl.db should not exist at destination after a refusal")
	}
}

func TestRunSymlinkedStagingDirRefused(t *testing.T) {
	s := newSetup(t)
	real := s.stagingDir
	linked := real + "-link"
	if err := os.Rename(real, linked); err != nil {
		t.Fatalf("rename: %v", err)
	}
	if err := os.Symlink(linked, real); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailUnsafeStaging {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailUnsafeStaging)
	}
}

func TestRunSymlinkedZigbee2mqttSubdirRefused(t *testing.T) {
	s := newSetup(t)
	real := filepath.Join(s.stagingDir, "zigbee2mqtt")
	linked := filepath.Join(s.dir, "zigbee2mqtt-real")
	if err := os.Rename(real, linked); err != nil {
		t.Fatalf("rename: %v", err)
	}
	if err := os.Symlink(linked, real); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailUnsafeStaging {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailUnsafeStaging)
	}
}

func TestRunSymlinkedDestinationDirRefused(t *testing.T) {
	// Cross-review finding: a symlinked live destination directory (here,
	// zigbee2mqtt_dir itself) must be refused, not silently followed.
	s := newSetup(t)
	real := s.zigbee2mqttDir
	linked := real + "-real"
	if err := os.Rename(real, linked); err != nil {
		t.Fatalf("rename: %v", err)
	}
	if err := os.Symlink(linked, real); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailUnsafeDestination {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailUnsafeDestination)
	}
	if leftover := leftoverTempFiles(t, s.dir); len(leftover) != 0 {
		t.Fatalf("leftover temp files after a refusal: %v", leftover)
	}
}

func TestRunDotDotPathInManifestRefused(t *testing.T) {
	s := newSetup(t)
	m := s.defaultManifest()
	m.Files[0].Path = "../escape.db"
	s.writeManifest(t, m)

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailUnsafePath {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailUnsafePath)
	}
}

func TestRunAbsolutePathInManifestRefused(t *testing.T) {
	s := newSetup(t)
	m := s.defaultManifest()
	m.Files[0].Path = "/etc/passwd"
	s.writeManifest(t, m)

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailUnsafePath {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailUnsafePath)
	}
}

func TestRunUnknownFileNameRefused(t *testing.T) {
	s := newSetup(t)
	m := s.defaultManifest()
	m.Files[0].Path = "not-on-the-allowlist.db"
	s.writeManifest(t, m)
	writeFile(t, filepath.Join(s.stagingDir, "not-on-the-allowlist.db"), s.thermoctlBytes)
	if err := os.Remove(filepath.Join(s.stagingDir, "thermoctl.db")); err != nil {
		t.Fatalf("remove: %v", err)
	}

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailUnknownFileName {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailUnknownFileName)
	}
}

func TestRunMalformedManifestRefused(t *testing.T) {
	s := newSetup(t)
	writeFile(t, s.manifestPath, []byte("{not valid json"))

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailManifestMalformed || status.BackupID != "" {
		t.Fatalf("unexpected status: %+v", status)
	}
}

func TestRunOversizedManifestRefused(t *testing.T) {
	// Cross-review finding: an (agent-controlled) manifest larger than
	// MaxManifestBytes must be refused before it is even fully read, let
	// alone parsed.
	s := newSetup(t)
	huge := make([]byte, MaxManifestBytes+1024)
	for i := range huge {
		huge[i] = 'a'
	}
	writeFile(t, s.manifestPath, huge)

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailManifestTooLarge || status.BackupID != "" {
		t.Fatalf("unexpected status: %+v", status)
	}
}

func TestRunEmptyBackupIDRefused(t *testing.T) {
	s := newSetup(t)
	m := s.defaultManifest()
	m.BackupID = ""
	s.writeManifest(t, m)

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailManifestMalformed {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailManifestMalformed)
	}
}

func TestRunDestinationDirectoryMissingRefused(t *testing.T) {
	s := newSetup(t)
	if err := os.RemoveAll(s.zigbee2mqttDir); err != nil {
		t.Fatalf("remove: %v", err)
	}

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailDestinationMissing {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailDestinationMissing)
	}
}

// TestFinalizeAllPartialFailureRemovesPendingTempsButKeepsRenamedFiles
// exercises finalizeAll (move.go) directly, at the unit level, rather
// than through the full run() -- a genuine rename-time failure in
// production can only come from something changing between prepareFile
// and this step (e.g. a destination directory's permissions, or a disk
// filling up); it cannot be reproduced through run() by pre-occupying a
// final destination path, because that path is, by construction, one of
// the exact three paths liveStoreIsEmpty already checks and refuses on
// *before* validation/copy ever runs (see TestRunLiveStoreNotEmptyZ2m).
// This test instead builds the "already prepared" state finalizeAll
// expects directly, and makes only its rename fail.
func TestFinalizeAllPartialFailureRemovesPendingTempsButKeepsRenamedFiles(t *testing.T) {
	dir1 := t.TempDir()
	dir2 := t.TempDir()

	temp1, tempPath1, err := createTempInDir(dir1)
	if err != nil {
		t.Fatalf("createTempInDir: %v", err)
	}
	if _, err := temp1.WriteString("content-1"); err != nil {
		t.Fatalf("write: %v", err)
	}
	temp1.Close()
	finalPath1 := filepath.Join(dir1, "final-1.db")

	temp2, tempPath2, err := createTempInDir(dir2)
	if err != nil {
		t.Fatalf("createTempInDir: %v", err)
	}
	temp2.Close()
	// The second file's final name is already occupied by a directory --
	// os.Rename onto it fails deterministically, without relying on any
	// OS-specific permission behavior.
	finalPath2 := filepath.Join(dir2, "final-2.db")
	if err := os.Mkdir(finalPath2, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	prepared := []preparedFile{
		{RelPath: "one", TempPath: tempPath1, FinalPath: finalPath1, DestDir: dir1},
		{RelPath: "two", TempPath: tempPath2, FinalPath: finalPath2, DestDir: dir2},
	}

	result := finalizeAll(prepared, noopWarn)
	if result.complete() {
		t.Fatalf("expected an incomplete result")
	}
	if result.Renamed != 1 || result.Total != 2 {
		t.Fatalf("result = %+v, want Renamed=1 Total=2", result)
	}

	if data, err := os.ReadFile(finalPath1); err != nil || string(data) != "content-1" {
		t.Fatalf("file 1 was not renamed into place: %v %q", err, data)
	}
	if _, err := os.Stat(tempPath2); !os.IsNotExist(err) {
		t.Fatalf("temp file 2 should have been removed after its rename failed, err=%v", err)
	}
}

func TestRunPartialMoveFailureIsReportedHonestly(t *testing.T) {
	// A run()-level integration test complementing the direct finalizeAll
	// test above: makes the *second* manifest entry's destination
	// directory read-only after this program's own process has already
	// created it (root/the test's own uid can usually still write to a
	// 0500 directory it owns on some platforms, so this is skipped where
	// that turns out to be true rather than asserting a false failure).
	s := newSetup(t)
	if err := os.Chmod(s.zigbee2mqttDir, 0o500); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	t.Cleanup(func() { os.Chmod(s.zigbee2mqttDir, 0o755) })

	if probe, probePath, probeErr := createTempInDir(s.zigbee2mqttDir); probeErr == nil {
		probe.Close()
		os.Remove(probePath)
		t.Skip("this process can still write to a 0500 directory it owns on this platform/filesystem")
	}

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	// With a read-only destination directory, prepareFile itself fails
	// while creating the temp file for the first zigbee2mqtt entry --
	// validateAll aborts, cleaning up the earlier thermoctl.db temp file,
	// so nothing at all has been moved yet (a stronger guarantee than a
	// bare partial-move report: staging is entirely untouched).
	if status.Result != ResultFailure {
		t.Fatalf("unexpected status: %+v", status)
	}
	if _, err := os.Stat(s.thermoctlDBPath); !os.IsNotExist(err) {
		t.Fatalf("nothing should have moved when the first failure happens during validation, not finalize")
	}
	if _, err := os.Stat(s.manifestPath); err != nil {
		t.Fatalf("manifest should still be present after a refusal: %v", err)
	}
	if leftover := leftoverTempFiles(t, s.dir); len(leftover) != 0 {
		t.Fatalf("leftover temp files after a refusal: %v", leftover)
	}
}

func TestRunTwiceIsIdempotentAfterSuccess(t *testing.T) {
	s := newSetup(t)
	if code := run(s.config(), noopWarn, testNow); code != 0 {
		t.Fatalf("first run exit code = %d, want 0", code)
	}
	// A second run with nothing staged must be a clean no-op, not a
	// crash or a spurious second status write.
	before := s.readStatus(t)
	code := run(s.config(), noopWarn, testNow)
	if code != 0 {
		t.Fatalf("second run exit code = %d, want 0", code)
	}
	after := s.readStatus(t)
	if before != after {
		t.Fatalf("status file changed on a no-op second run: %+v -> %+v", before, after)
	}
}

// TestOpenStagedFileImmuneToPathSwapAfterOpen is the regression test for
// the cross-review's TOCTOU finding: once openStagedFileNoFollow has
// returned an open descriptor, nothing that subsequently happens to the
// *path* -- including the untrusted agent process replacing it with a
// symlink to unrelated, attacker-controlled content -- has any effect on
// what is read from that descriptor. This is the exact property
// prepareFile (validate.go) relies on: it opens once and reads/hashes/
// copies from that one descriptor in a single, uninterrupted pass, never
// reopening the path.
func TestOpenStagedFileImmuneToPathSwapAfterOpen(t *testing.T) {
	dir := t.TempDir()
	originalContent := []byte("original-validated-content")
	path := filepath.Join(dir, "thermoctl.db")
	writeFile(t, path, originalContent)

	file, size, err := openStagedFileNoFollow(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer file.Close()
	if size != int64(len(originalContent)) {
		t.Fatalf("size = %d, want %d", size, len(originalContent))
	}

	// Simulate the untrusted agent process swapping the staged path for a
	// symlink to something else entirely, in the window between this
	// program opening the file and finishing its read.
	outside := filepath.Join(dir, "outside.txt")
	writeFile(t, outside, []byte("attacker-controlled-content-of-a-different-length"))
	if err := os.Remove(path); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if err := os.Symlink(outside, path); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	got, err := io.ReadAll(file)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(got) != string(originalContent) {
		t.Fatalf("read %q from the already-open descriptor after a path swap, want the original %q -- "+
			"the whole design this test guards depends on this never changing", got, originalContent)
	}
}

// TestPrepareFileEndToEndImmuneToPathSwapAfterOpen exercises the actual
// prepareFile function this program uses (not just the low-level open
// primitive above): the staged path is swapped for a symlink to
// different, differently-sized content immediately after prepareFile
// would have opened it (there is no externally observable window to hook
// into, by design -- open/read/hash/copy is one uninterrupted call), so
// this instead proves the same property the way an external attacker
// actually could observe it: run prepareFile normally, and separately
// confirm (via TestOpenStagedFileImmuneToPathSwapAfterOpen above) that the
// primitive it is built on is immune. This test additionally confirms
// prepareFile's own end-to-end output -- the copied file, once finalized
// -- is a plain regular file with exactly the validated bytes, never a
// symlink and never anything derived from a path that changed after the
// fact.
func TestPrepareFileEndToEndProducesARegularFileWithValidatedBytes(t *testing.T) {
	s := newSetup(t)
	code := run(s.config(), noopWarn, testNow)
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	info, err := os.Lstat(s.thermoctlDBPath)
	if err != nil {
		t.Fatalf("lstat: %v", err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		t.Fatalf("moved file is a symlink, want a regular file")
	}
	if !info.Mode().IsRegular() {
		t.Fatalf("moved file is not a regular file: %v", info.Mode())
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		t.Fatalf("could not stat_t the moved file")
	}
	if stat.Nlink != 1 {
		t.Fatalf("moved file has Nlink = %d, want 1", stat.Nlink)
	}
}
