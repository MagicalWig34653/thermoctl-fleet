package main

import (
	"os"
	"path/filepath"
	"testing"
)

// -- P5.5d cross-review fix: the intermediate-path-component symlink gap
// (stagingroot.go) -- regression tests reproducing the reviewer's own
// demonstrated attacks against the *previous* (path-join-based)
// removeStagingContents/prepareFile, and confirming StagingRoot closes
// them. ---------------------------------------------------------------

// openTestStagingRoot is a small helper: opens sr, fails the test if that
// does not succeed, and registers its own cleanup.
func openTestStagingRoot(t *testing.T, dir string) *StagingRoot {
	t.Helper()
	sr, err := openStagingRoot(dir)
	if err != nil || sr == nil {
		t.Fatalf("openStagingRoot(%q): sr=%v err=%v", dir, sr, err)
	}
	t.Cleanup(func() { sr.Close() })
	return sr
}

// validateAndFinalize runs the same two production steps run() itself
// runs, back to back, for tests that need a fully-populated live
// destination before they can exercise a *later* step (cleanup) in
// isolation.
func validateAndFinalize(t *testing.T, sr *StagingRoot, manifest *Manifest, targets Targets) []preparedFile {
	t.Helper()
	prepared, err := validateAll(sr, manifest, targets)
	if err != nil {
		t.Fatalf("validateAll: %v", err)
	}
	result := finalizeAll(prepared, noopWarn)
	if !result.complete() {
		t.Fatalf("finalizeAll did not complete: %+v", result)
	}
	return prepared
}

// TestRemoveStagingContentsImmuneToZigbeeSubdirSwapBeforeCleanup is the
// reviewer's own first demonstrated attack: after this program has
// already validated and moved every file into its live destination
// (validateAll/finalizeAll, which along the way opens and caches a
// sub-root for "zigbee2mqtt" the first time a zigbee2mqtt entry is
// touched), the untrusted agent process swaps staging's own
// "zigbee2mqtt" subdirectory for a symlink into the very live directory
// this program just populated. The previous design's cleanup step
// (`os.Lstat`/`os.Remove` on a freshly joined
// "stagingDir/zigbee2mqtt/database.db" path) would have silently
// followed that swapped intermediate component and deleted the live
// file. removeStagingContents must not: it already holds the
// pre-swap sub-root from the validation pass, so its own per-file
// removals are unaffected by the swap, and the final
// removeZigbeeDirIfPresent call (through the top root, a single
// component) sees the symlink and refuses to remove it.
func TestRemoveStagingContentsImmuneToZigbeeSubdirSwapBeforeCleanup(t *testing.T) {
	s := newSetup(t)
	sr := openTestStagingRoot(t, s.stagingDir)
	manifest := s.defaultManifest()
	targets := Targets{ThermoctlDBPath: s.thermoctlDBPath, Zigbee2mqttDir: s.zigbee2mqttDir}
	validateAndFinalize(t, sr, &manifest, targets)

	// Every live file now exists. Swap staging's own "zigbee2mqtt" for a
	// symlink into the live directory this program just populated --
	// simulating the untrusted agent doing so between this program's
	// validation pass and its cleanup step.
	zigbeeStagingPath := filepath.Join(s.stagingDir, "zigbee2mqtt")
	if err := os.RemoveAll(zigbeeStagingPath); err != nil {
		t.Fatalf("remove real zigbee2mqtt staging subdir: %v", err)
	}
	if err := os.Symlink(s.zigbee2mqttDir, zigbeeStagingPath); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	// Cleanup must not silently succeed through the swapped symlink --
	// it refuses at the final directory-removal step (the symlink is not
	// a real directory), but must never have touched the live directory
	// through it either way.
	if err := removeStagingContents(sr, &manifest); err == nil {
		t.Fatalf("expected removeStagingContents to refuse once zigbee2mqtt was swapped for a symlink into the live directory")
	}

	// The live files this program already restored must be completely
	// untouched.
	liveDB, err := os.ReadFile(filepath.Join(s.zigbee2mqttDir, "database.db"))
	if err != nil || string(liveDB) != string(s.z2mDBBytes) {
		t.Fatalf("live database.db was deleted or corrupted: %v %q", err, liveDB)
	}
	liveBackup, err := os.ReadFile(filepath.Join(s.zigbee2mqttDir, "coordinator_backup.json"))
	if err != nil || string(liveBackup) != string(s.z2mBackupBytes) {
		t.Fatalf("live coordinator_backup.json was deleted or corrupted: %v %q", err, liveBackup)
	}

	// The symlink itself must still be exactly what the attacker planted
	// -- never followed, never removed as if it were the real directory.
	info, err := os.Lstat(zigbeeStagingPath)
	if err != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatalf("the planted symlink at staging's own zigbee2mqtt path was itself altered: %v %v", err, info)
	}
}

// TestStagingRootOperationsImmuneToStagingDirEntrySwapAfterOpen is the
// reviewer's second demonstrated attack: the staging directory's own
// entry, in *its* parent, gets renamed away and replaced by a symlink
// into a live directory, after this program already opened it as a
// *StagingRoot. Every later operation through that already-open root
// (validation, finalize, cleanup) must keep operating on the original,
// real directory -- never on whatever the swapped-in symlink at its old
// name now resolves to.
func TestStagingRootOperationsImmuneToStagingDirEntrySwapAfterOpen(t *testing.T) {
	s := newSetup(t)
	sr := openTestStagingRoot(t, s.stagingDir)
	manifest := s.defaultManifest()
	targets := Targets{ThermoctlDBPath: s.thermoctlDBPath, Zigbee2mqttDir: s.zigbee2mqttDir}
	validateAndFinalize(t, sr, &manifest, targets)

	// Swap the staging directory's own entry for a symlink into the live
	// zigbee2mqtt directory this program just populated.
	moved := s.stagingDir + "-moved-elsewhere"
	if err := os.Rename(s.stagingDir, moved); err != nil {
		t.Fatalf("rename: %v", err)
	}
	t.Cleanup(func() { os.RemoveAll(moved) })
	if err := os.Symlink(s.zigbee2mqttDir, s.stagingDir); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	t.Cleanup(func() { os.Remove(s.stagingDir) })

	if err := removeStagingContents(sr, &manifest); err != nil {
		t.Fatalf("removeStagingContents after a staging-dir entry swap: %v", err)
	}

	// The live directory -- now also reachable via the swapped-in
	// symlink at the staging dir's own former name -- must still hold
	// exactly the two files this program restored: nothing was deleted
	// through it.
	if _, err := os.Stat(filepath.Join(s.zigbee2mqttDir, "database.db")); err != nil {
		t.Fatalf("live database.db missing after cleanup: %v", err)
	}
	if _, err := os.Stat(filepath.Join(s.zigbee2mqttDir, "coordinator_backup.json")); err != nil {
		t.Fatalf("live coordinator_backup.json missing after cleanup: %v", err)
	}

	// The *real*, moved-away original staging directory must have had
	// its own contents actually cleared by cleanup -- proving sr kept
	// operating on the original directory, not silently becoming a
	// no-op once its old name pointed elsewhere.
	entries, err := os.ReadDir(moved)
	if err != nil {
		t.Fatalf("reading the original (moved) staging directory: %v", err)
	}
	if len(entries) != 0 {
		t.Fatalf("original staging directory still has entries after cleanup: %v", entries)
	}
}

// TestValidateAllRefusesSymlinkedZigbeeSubdirWithMatchingHashDecoy is the
// reviewer's third demonstrated attack: "zigbee2mqtt" is *already* a
// symlink -- pointing at a decoy directory whose files have been crafted
// to match the manifest's own size/sha256 exactly -- before validation
// ever runs. Must be refused outright, and nothing from the decoy may
// ever reach a live destination (the hash matching the manifest must not
// matter: this program never even opens the decoy's files in the first
// place, since the symlink itself is refused before any per-file check).
func TestValidateAllRefusesSymlinkedZigbeeSubdirWithMatchingHashDecoy(t *testing.T) {
	s := newSetup(t)
	if err := os.RemoveAll(filepath.Join(s.stagingDir, "zigbee2mqtt")); err != nil {
		t.Fatalf("remove real zigbee2mqtt staging subdir: %v", err)
	}
	decoy := filepath.Join(s.dir, "decoy-zigbee2mqtt")
	if err := os.Mkdir(decoy, 0o755); err != nil {
		t.Fatalf("mkdir decoy: %v", err)
	}
	writeFile(t, filepath.Join(decoy, "database.db"), s.z2mDBBytes)
	writeFile(t, filepath.Join(decoy, "coordinator_backup.json"), s.z2mBackupBytes)
	if err := os.Symlink(decoy, filepath.Join(s.stagingDir, "zigbee2mqtt")); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	sr := openTestStagingRoot(t, s.stagingDir)
	manifest := s.defaultManifest()
	targets := Targets{ThermoctlDBPath: s.thermoctlDBPath, Zigbee2mqttDir: s.zigbee2mqttDir}

	if _, err := validateAll(sr, &manifest, targets); err == nil {
		t.Fatalf("expected validateAll to refuse a symlinked zigbee2mqtt subdirectory, even with matching-hash content behind it")
	}

	// Nothing from the decoy should have reached a live destination.
	if _, err := os.Stat(filepath.Join(s.zigbee2mqttDir, "database.db")); !os.IsNotExist(err) {
		t.Fatalf("a file was copied to the live destination despite the symlinked decoy: %v", err)
	}
	if _, err := os.Stat(filepath.Join(s.zigbee2mqttDir, "coordinator_backup.json")); !os.IsNotExist(err) {
		t.Fatalf("a file was copied to the live destination despite the symlinked decoy: %v", err)
	}
}

// TestValidateAllRefusesSymlinkPointingToAnotherStagedFile is the
// reviewer's fourth demonstrated requirement: a symlink *inside* staging
// that points at a *different, legitimate staged file* (a relative
// target that never leaves the staging root at all) must still be
// refused. os.Root's own escape protection does not help here by
// design -- the target never leaves the root -- so this exercises
// StagingRoot's own explicit Lstat-before-open/remove check, not Root's
// built-in behavior.
func TestValidateAllRefusesSymlinkPointingToAnotherStagedFile(t *testing.T) {
	s := newSetup(t)
	thermoctlPath := filepath.Join(s.stagingDir, "thermoctl.db")
	if err := os.Remove(thermoctlPath); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if err := os.Symlink("zigbee2mqtt/database.db", thermoctlPath); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	sr := openTestStagingRoot(t, s.stagingDir)
	manifest := s.defaultManifest()
	targets := Targets{ThermoctlDBPath: s.thermoctlDBPath, Zigbee2mqttDir: s.zigbee2mqttDir}

	_, err := validateAll(sr, &manifest, targets)
	if err == nil {
		t.Fatalf("expected validateAll to refuse a staged entry that is a symlink to another staged file")
	}
	if detailOf(err, "") != DetailNotRegularFile {
		t.Fatalf("error = %v, want detail %q", err, DetailNotRegularFile)
	}
}

// -- StagingRoot's own removal helpers, covered directly (P5.5d
// cross-review's "cover removeLeafEntry/removeEmptyDirEntry (or their
// replacements) fully" requirement) -------------------------------------

func TestStagingRootRemoveEntryRemovesAPresentTopLevelFile(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "thermoctl.db"), []byte("x"))
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeEntry("thermoctl.db"); err != nil {
		t.Fatalf("removeEntry: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "thermoctl.db")); !os.IsNotExist(err) {
		t.Fatalf("thermoctl.db still exists after removeEntry")
	}
}

func TestStagingRootRemoveEntryOnAbsentEntryIsANoOp(t *testing.T) {
	dir := t.TempDir()
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeEntry("thermoctl.db"); err != nil {
		t.Fatalf("removeEntry on an absent entry should be a no-op, got: %v", err)
	}
}

func TestStagingRootRemoveEntryRefusesADirectory(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "thermoctl.db"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeEntry("thermoctl.db"); err == nil {
		t.Fatalf("expected removeEntry to refuse an entry that is unexpectedly a directory")
	}
	if _, err := os.Stat(filepath.Join(dir, "thermoctl.db")); err != nil {
		t.Fatalf("the unexpected directory should have been left in place: %v", err)
	}
}

func TestStagingRootRemoveEntryOnZigbeeFileAbsentZigbeeDirIsANoOp(t *testing.T) {
	dir := t.TempDir()
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeEntry("zigbee2mqtt/database.db"); err != nil {
		t.Fatalf("removeEntry for a zigbee2mqtt file when zigbee2mqtt itself does not exist should be a no-op, got: %v", err)
	}
}

func TestStagingRootRemoveZigbeeDirIfPresentOnAbsentIsANoOp(t *testing.T) {
	dir := t.TempDir()
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeZigbeeDirIfPresent(); err != nil {
		t.Fatalf("expected a no-op for an absent zigbee2mqtt, got: %v", err)
	}
}

func TestStagingRootRemoveZigbeeDirIfPresentRefusesASymlink(t *testing.T) {
	dir := t.TempDir()
	elsewhere := t.TempDir()
	if err := os.Symlink(elsewhere, filepath.Join(dir, "zigbee2mqtt")); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeZigbeeDirIfPresent(); err == nil {
		t.Fatalf("expected removeZigbeeDirIfPresent to refuse a symlinked zigbee2mqtt")
	}
	if _, err := os.Lstat(filepath.Join(dir, "zigbee2mqtt")); err != nil {
		t.Fatalf("the symlink should have been left in place: %v", err)
	}
}

func TestStagingRootRemoveZigbeeDirIfPresentRefusesANonEmptyDir(t *testing.T) {
	dir := t.TempDir()
	zdir := filepath.Join(dir, "zigbee2mqtt")
	if err := os.Mkdir(zdir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	writeFile(t, filepath.Join(zdir, "leftover.txt"), []byte("x"))
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeZigbeeDirIfPresent(); err == nil {
		t.Fatalf("expected removeZigbeeDirIfPresent to refuse a non-empty directory")
	}
	if _, err := os.Stat(filepath.Join(zdir, "leftover.txt")); err != nil {
		t.Fatalf("leftover file should still be present: %v", err)
	}
}

func TestStagingRootRemoveZigbeeDirIfPresentRemovesAnEmptyDir(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "zigbee2mqtt"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeZigbeeDirIfPresent(); err != nil {
		t.Fatalf("removeZigbeeDirIfPresent: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "zigbee2mqtt")); !os.IsNotExist(err) {
		t.Fatalf("zigbee2mqtt should have been removed")
	}
}

func TestStagingRootRemoveManifestOnAbsentIsANoOp(t *testing.T) {
	dir := t.TempDir()
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeManifest(); err != nil {
		t.Fatalf("expected a no-op for an absent manifest, got: %v", err)
	}
}

func TestStagingRootRemoveManifestRefusesADirectory(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, ManifestFilename), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeManifest(); err == nil {
		t.Fatalf("expected removeManifest to refuse a directory named manifest.json")
	}
}

func TestStagingRootRemoveManifestRemovesIt(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, ManifestFilename), []byte("{}"))
	sr := openTestStagingRoot(t, dir)

	if err := sr.removeManifest(); err != nil {
		t.Fatalf("removeManifest: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, ManifestFilename)); !os.IsNotExist(err) {
		t.Fatalf("manifest.json should have been removed")
	}
}

// -- StagingRoot's own open/lstat helpers, covered directly ---------------

func TestStagingRootOpenEntryNoFollowRefusesHardLinkedFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "thermoctl.db")
	writeFile(t, path, []byte("x"))
	link := filepath.Join(dir, "another-name-for-the-same-inode")
	if err := os.Link(path, link); err != nil {
		t.Skipf("hard links not supported on this filesystem: %v", err)
	}
	sr := openTestStagingRoot(t, dir)

	_, _, err := sr.openEntryNoFollow("thermoctl.db")
	if err != errHardLinked {
		t.Fatalf("err = %v, want errHardLinked", err)
	}
}

func TestStagingRootOpenEntryNoFollowAcceptsASingleLinkFile(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "thermoctl.db"), []byte("hello"))
	sr := openTestStagingRoot(t, dir)

	file, size, err := sr.openEntryNoFollow("thermoctl.db")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer file.Close()
	if size != 5 {
		t.Fatalf("size = %d, want 5", size)
	}
}

func TestStagingRootOpenEntryNoFollowRefusesSymlink(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "target.db")
	writeFile(t, target, []byte("x"))
	if err := os.Symlink(target, filepath.Join(dir, "thermoctl.db")); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	sr := openTestStagingRoot(t, dir)

	_, _, err := sr.openEntryNoFollow("thermoctl.db")
	if err != errNotRegular {
		t.Fatalf("err = %v, want errNotRegular", err)
	}
}

func TestStagingRootOpenEntryNoFollowMissingEntry(t *testing.T) {
	dir := t.TempDir()
	sr := openTestStagingRoot(t, dir)

	_, _, err := sr.openEntryNoFollow("thermoctl.db")
	if !os.IsNotExist(err) {
		t.Fatalf("err = %v, want IsNotExist", err)
	}
}

// -- Post-open re-check (P5.5d cross-review follow-up, docs/STATUS.md):
// openEntryNoFollow's Lstat and os.Root's own OpenFile are two separate
// calls, and Root -- unlike a plain os.OpenFile with O_NOFOLLOW -- follows
// an in-root symlink. These tests use testPostLstatHook (stagingroot.go)
// to deterministically land the swap in the exact window between the two
// calls, which real timing could otherwise only hit by luck. -------------

// withPostLstatHook installs hook as testPostLstatHook for the duration
// of the calling test and restores nil afterwards -- every test below
// uses this so a failure never leaks the hook into a later, unrelated
// test.
func withPostLstatHook(t *testing.T, hook func()) {
	t.Helper()
	testPostLstatHook = hook
	t.Cleanup(func() { testPostLstatHook = nil })
}

func TestStagingRootOpenEntryNoFollowRefusesSymlinkSwappedInBetweenLstatAndOpen(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "thermoctl.db")
	writeFile(t, path, []byte("real-staged-bytes"))
	// decoy lives INSIDE staging, at a relative target -- os.Root follows
	// this (it never leaves the root), which is exactly the gap being
	// closed: Root's own escape protection does not help here.
	writeFile(t, filepath.Join(dir, "decoy.db"), []byte("decoy-bytes"))
	sr := openTestStagingRoot(t, dir)

	withPostLstatHook(t, func() {
		if err := os.Remove(path); err != nil {
			t.Fatalf("remove real entry before swap: %v", err)
		}
		if err := os.Symlink("decoy.db", path); err != nil {
			t.Fatalf("symlink swap: %v", err)
		}
	})

	file, _, err := sr.openEntryNoFollow("thermoctl.db")
	if err == nil {
		file.Close()
		t.Fatalf("expected openEntryNoFollow to refuse an entry swapped for an in-root symlink between its Lstat and its Open")
	}
}

func TestStagingRootOpenEntryNoFollowRefusesHardLinkSwappedInBetweenLstatAndOpen(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "thermoctl.db")
	writeFile(t, path, []byte("real-staged-bytes"))
	decoy := filepath.Join(dir, "decoy.db")
	writeFile(t, decoy, []byte("decoy-bytes"))
	sr := openTestStagingRoot(t, dir)

	withPostLstatHook(t, func() {
		if err := os.Remove(path); err != nil {
			t.Fatalf("remove real entry before swap: %v", err)
		}
		if err := os.Link(decoy, path); err != nil {
			t.Skipf("hard links not supported on this filesystem: %v", err)
		}
	})

	file, _, err := sr.openEntryNoFollow("thermoctl.db")
	if err == nil {
		file.Close()
		t.Fatalf("expected openEntryNoFollow to refuse an entry swapped for a second hard link between its Lstat and its Open")
	}
}

func TestStagingRootOpenEntryNoFollowAcceptsAnUnswappedFileEvenWithHookInstalled(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "thermoctl.db"), []byte("hello"))
	sr := openTestStagingRoot(t, dir)

	hookRan := false
	withPostLstatHook(t, func() { hookRan = true })

	file, size, err := sr.openEntryNoFollow("thermoctl.db")
	if err != nil {
		t.Fatalf("unexpected error for a normal, unswapped file: %v", err)
	}
	defer file.Close()
	if size != 5 {
		t.Fatalf("size = %d, want 5", size)
	}
	if !hookRan {
		t.Fatalf("testPostLstatHook was never invoked -- test would not actually exercise the race window")
	}
}

func TestReadManifestBytesRefusesSymlinkSwappedInBetweenLstatAndOpen(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ManifestFilename)
	writeFile(t, path, []byte(`{"backup_id":"x","staged_at":"x","files":[]}`))
	writeFile(t, filepath.Join(dir, "decoy-manifest.json"), []byte(`{"backup_id":"decoy","staged_at":"x","files":[]}`))
	sr := openTestStagingRoot(t, dir)

	withPostLstatHook(t, func() {
		if err := os.Remove(path); err != nil {
			t.Fatalf("remove real manifest before swap: %v", err)
		}
		if err := os.Symlink("decoy-manifest.json", path); err != nil {
			t.Fatalf("symlink swap: %v", err)
		}
	})

	if _, _, err := sr.readManifestBytes(); err == nil {
		t.Fatalf("expected readManifestBytes to refuse manifest.json swapped for an in-root symlink between its Lstat and its Open")
	}
}

func TestReadManifestBytesRefusesHardLinkSwappedInBetweenLstatAndOpen(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ManifestFilename)
	writeFile(t, path, []byte(`{"backup_id":"x","staged_at":"x","files":[]}`))
	decoy := filepath.Join(dir, "decoy-manifest.json")
	writeFile(t, decoy, []byte(`{"backup_id":"decoy","staged_at":"x","files":[]}`))
	sr := openTestStagingRoot(t, dir)

	withPostLstatHook(t, func() {
		if err := os.Remove(path); err != nil {
			t.Fatalf("remove real manifest before swap: %v", err)
		}
		if err := os.Link(decoy, path); err != nil {
			t.Skipf("hard links not supported on this filesystem: %v", err)
		}
	})

	if _, _, err := sr.readManifestBytes(); err == nil {
		t.Fatalf("expected readManifestBytes to refuse manifest.json swapped for a second hard link between its Lstat and its Open")
	}
}

func TestReadManifestBytesAcceptsAnUnswappedManifestEvenWithHookInstalled(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, ManifestFilename), []byte(`{"backup_id":"x","staged_at":"x","files":[]}`))
	sr := openTestStagingRoot(t, dir)

	hookRan := false
	withPostLstatHook(t, func() { hookRan = true })

	data, present, err := sr.readManifestBytes()
	if err != nil || !present {
		t.Fatalf("unexpected result for a normal, unswapped manifest: present=%v err=%v", present, err)
	}
	if len(data) == 0 {
		t.Fatalf("expected non-empty manifest bytes")
	}
	if !hookRan {
		t.Fatalf("testPostLstatHook was never invoked -- test would not actually exercise the race window")
	}
}

func TestStagingRootZigbeeRootRefusesAFileNamedZigbee2mqtt(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "zigbee2mqtt"), []byte("not a directory"))
	sr := openTestStagingRoot(t, dir)

	_, present, err := sr.zigbeeRoot()
	if err == nil {
		t.Fatalf("expected zigbeeRoot to refuse a plain file named zigbee2mqtt")
	}
	if present {
		t.Fatalf("present = true, want false alongside an error")
	}
}

func TestOpenStagingRootReturnsNilForAnAbsentDirectory(t *testing.T) {
	dir := t.TempDir()
	sr, err := openStagingRoot(filepath.Join(dir, "does-not-exist"))
	if err != nil {
		t.Fatalf("unexpected error for an absent staging directory: %v", err)
	}
	if sr != nil {
		t.Fatalf("expected a nil StagingRoot for an absent staging directory")
	}
}

func TestOpenStagingRootRefusesASymlink(t *testing.T) {
	dir := t.TempDir()
	real := filepath.Join(dir, "real")
	if err := os.Mkdir(real, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(real, link); err != nil {
		t.Fatalf("symlink: %v", err)
	}

	sr, err := openStagingRoot(link)
	if err == nil {
		t.Fatalf("expected openStagingRoot to refuse a symlinked staging directory")
	}
	if sr != nil {
		t.Fatalf("expected a nil StagingRoot alongside the error")
	}
}
