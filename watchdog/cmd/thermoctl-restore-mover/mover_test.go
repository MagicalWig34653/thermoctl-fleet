package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
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
	z2mDB, err := os.ReadFile(filepath.Join(s.zigbee2mqttDir, "database.db"))
	if err != nil || string(z2mDB) != string(s.z2mDBBytes) {
		t.Fatalf("zigbee2mqtt/database.db not moved correctly: %v %q", err, z2mDB)
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

func TestRunCrossFilesystemSimulatedRefused(t *testing.T) {
	// sameDevice is exercised directly (as documented in validate.go) --
	// simulating a genuine EXDEV via two real, distinct mounted
	// filesystems is not reliably available in a CI sandbox, so this
	// test constructs two *syscall.Stat_t-backed os.FileInfo values with
	// different Dev fields the same way validateOneFile's own call would
	// see them, via two temp dirs and monkeypatching is avoided in favor
	// of a direct unit test of sameDevice itself (see validate_test.go).
	t.Skip("covered directly by TestSameDeviceDiffers in validate_test.go")
}

func TestRunPartialMoveFailureLeavesStagingIntact(t *testing.T) {
	s := newSetup(t)
	// Make the second manifest entry's destination directory read-only so
	// its rename fails after the first file has already moved.
	if err := os.Chmod(s.zigbee2mqttDir, 0o500); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	t.Cleanup(func() { os.Chmod(s.zigbee2mqttDir, 0o755) })

	code := run(s.config(), noopWarn, testNow)
	if code != 1 {
		t.Fatalf("exit code = %d, want 1", code)
	}
	status := s.readStatus(t)
	if status.Detail != DetailPartialMove {
		t.Fatalf("detail = %q, want %q", status.Detail, DetailPartialMove)
	}

	// thermoctl.db (validated first) should have moved; the
	// zigbee2mqtt files, and the manifest, should still be staged.
	if _, err := os.Stat(s.thermoctlDBPath); err != nil {
		t.Fatalf("thermoctl.db should have moved before the failure: %v", err)
	}
	if _, err := os.Stat(s.manifestPath); err != nil {
		t.Fatalf("manifest should still be present after a partial failure: %v", err)
	}
	if _, err := os.Stat(filepath.Join(s.stagingDir, "zigbee2mqtt", "database.db")); err != nil {
		t.Fatalf("unmoved file should still be staged: %v", err)
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
