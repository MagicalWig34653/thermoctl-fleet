package main

import (
	"fmt"
	"os"
	pathpkg "path"
)

// finalizeResult is what finalizeAll reports back to main.go: how many
// of the already-copied, already-verified temporary files were actually
// renamed into their final names (for the log line only -- the status
// file itself only ever carries one of the closed DETAIL_* strings, never
// a count).
type finalizeResult struct {
	Renamed int
	Total   int
}

func (r finalizeResult) complete() bool { return r.Renamed == r.Total }

// finalizeAll renames every already-validated, already-copied temporary
// file (preparedFile, created by validateAll/prepareFile inside its real
// destination directory) into its final name -- always a same-directory
// rename (the temp file was created in that exact directory), so this can
// never hit EXDEV: the cross-filesystem case that the previous,
// direct-staged-file-rename design had to guard against does not exist
// once the bytes are already copied into a temp file living in the
// destination directory itself.
//
// **Stops at the first failure and returns immediately** -- whatever was
// already renamed stays renamed (each os.Rename is already atomic and
// complete by the time it returns success, and the data was already
// fully verified before this function ever ran), and whatever was not
// yet renamed has its still-pending temp file removed (never left behind
// as an orphan) while the corresponding staged file is left in
// `stagingDir`, untouched, for a later run to finish once the underlying
// problem (e.g. a full disk) is fixed.
//
// On full completion, fsyncs every distinct destination directory once,
// so the renames are durable before this program tells the caller to
// remove the staging directory.
func finalizeAll(prepared []preparedFile, warn func(format string, args ...any)) finalizeResult {
	result := finalizeResult{Total: len(prepared)}
	touchedDirs := map[string]struct{}{}

	for i, one := range prepared {
		if err := os.Rename(one.TempPath, one.FinalPath); err != nil {
			warn("thermoctl-restore-mover: renaming %s to %s failed: %v", one.TempPath, one.FinalPath, err)
			// Every temp file from this point on (including this one, if
			// the rename itself failed without consuming it) is still
			// just a temp file -- clean them all up, never leave an
			// orphan behind in a live directory.
			for _, remaining := range prepared[i:] {
				os.Remove(remaining.TempPath)
			}
			return result
		}
		result.Renamed++
		touchedDirs[one.DestDir] = struct{}{}
	}

	for dir := range touchedDirs {
		fsyncDir(dir, warn)
	}
	return result
}

func fsyncDir(dir string, warn func(format string, args ...any)) {
	handle, err := os.Open(dir)
	if err != nil {
		warn("thermoctl-restore-mover: opening %s to fsync it failed: %v", dir, err)
		return
	}
	defer handle.Close()
	if err := handle.Sync(); err != nil {
		warn("thermoctl-restore-mover: fsyncing %s failed: %v", dir, err)
	}
}

// removeStagingContents removes exactly the entries this program itself
// validated and copied out of the staging directory -- the three
// allowlisted staged files (whichever are present), the fixed
// "zigbee2mqtt" subdirectory once it is empty, and manifest.json itself --
// called only once finalizeResult.complete() is true, so a later restore
// can stage again (agent/restore.py::_staged_restore_already_pending
// checks exactly this directory's manifest for its own "already staged"
// refusal, and only the manifest's absence, not the staging directory's
// own absence).
//
// **P5.5d: never removes the staging directory entry itself** (the
// previous design's `os.RemoveAll(stagingDir)`, cross-review open point --
// see docs/STATUS.md's P5.5c merge entry) -- removing a directory *entry*
// requires write permission on its *parent*, which is why the previous
// design needed `/var/lib/thermoctl-agent` (the staging directory's
// parent, which also holds the agent's own device token and age identity)
// in this program's ReadWritePaths=. Removing only the *contents* of an
// already-writable directory needs write permission on that directory
// itself only, never on its parent -- so this program's ReadWritePaths=
// can now name exactly the staging directory
// (/var/lib/thermoctl-agent/pending-restore), narrowing what a bug in
// this root-running program could ever reach. agent/restore.py's own
// `_create_and_verify_safe_dir`/`_assert_staging_layout_is_safe` are
// unaffected -- an already-existing, empty staging directory is exactly
// what `mkdir(..., exist_ok=True)` already tolerates.
//
// **Routed entirely through the held sr, never a joined path (second
// P5.5d fix, cross-review of the first): a plain
// `os.Lstat(filepath.Join(stagingDir, "zigbee2mqtt/database.db"))` looked
// safe (an Lstat immediately before an Remove) but was not -- Lstat and
// Remove on a *multi-component* path both resolve every *intermediate*
// component normally, protecting only the *final* one. If the agent swaps
// "zigbee2mqtt" itself for a symlink into a live directory between this
// program's earlier validation pass and this later cleanup pass, both the
// Lstat check and the Remove call would silently follow it and delete the
// live file this program had just restored.** `StagingRoot.removeEntry`/
// `removeZigbeeDirIfPresent`/`removeManifest` (stagingroot.go) close this
// by resolving "zigbee2mqtt" itself exactly once per run, into its own
// held sub-root, and only ever performing single-component operations
// through the top root or that sub-root afterward -- see stagingroot.go's
// own top docstring for the full reasoning, including why a
// single-component removal (POSIX `unlink(2)`/`rmdir(2)` never
// dereferences its own final named component) is safe by construction
// even without the sub-root, while a *multi*-component one is not.
func removeStagingContents(sr *StagingRoot, manifest *Manifest) error {
	needsZigbee := false
	for _, entry := range manifest.Files {
		if err := sr.removeEntry(entry.Path); err != nil {
			return fmt.Errorf("removing staged file %s: %w", entry.Path, err)
		}
		if dirPart, _ := pathpkg.Split(entry.Path); dirPart != "" {
			needsZigbee = true
		}
	}
	if needsZigbee {
		if err := sr.removeZigbeeDirIfPresent(); err != nil {
			return fmt.Errorf("removing staging subdirectory %s: %w", zigbeeDirName, err)
		}
	}
	if err := sr.removeManifest(); err != nil {
		return fmt.Errorf("removing manifest %s: %w", ManifestFilename, err)
	}
	return nil
}
