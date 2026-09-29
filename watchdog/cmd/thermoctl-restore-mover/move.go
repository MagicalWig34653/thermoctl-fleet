package main

import (
	"fmt"
	"os"
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

// removeStagingDir removes the whole staging directory, manifest
// included -- called only once finalizeResult.complete() is true, so a
// later restore can stage again
// (agent/restore.py::_staged_restore_already_pending checks exactly this
// directory's manifest for its own "already staged" refusal).
func removeStagingDir(stagingDir string) error {
	if err := os.RemoveAll(stagingDir); err != nil {
		return fmt.Errorf("removing staging directory %s: %w", stagingDir, err)
	}
	return nil
}
