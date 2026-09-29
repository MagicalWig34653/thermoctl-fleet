package main

import (
	"fmt"
	"os"
	pathpkg "path"
	"path/filepath"
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
// **lstat-checked, not blindly path-based removal** -- every entry is
// `os.Lstat`ed immediately before its own `os.Remove` and refused if it
// is not exactly the type this function expects there (a leaf entry must
// not turn out to be a directory; the "zigbee2mqtt" entry must be a real,
// non-symlink directory). This is defense in depth on top of an operation
// that is already safe against a symlink swap for the *entry itself* --
// POSIX `unlink(2)`/`rmdir(2)` (what `os.Remove` calls, depending on the
// entry's type) never dereference the final path component being
// removed, so even an entry the untrusted agent process swapped for a
// symlink pointing into a live directory is removed as *just the
// symlink*, never traversed into. `rmdir(2)` additionally refuses
// outright (`ENOTEMPTY`) if anything unexpected still exists inside
// "zigbee2mqtt" -- this function only ever calls it after already
// removing exactly the files the manifest named inside that
// subdirectory, never a forced/recursive removal.
func removeStagingContents(stagingDir string, manifest *Manifest) error {
	subdirs := map[string]struct{}{}
	for _, entry := range manifest.Files {
		full := filepath.Join(stagingDir, filepath.FromSlash(entry.Path))
		if err := removeLeafEntry(full); err != nil {
			return fmt.Errorf("removing staged file %s: %w", entry.Path, err)
		}
		if dirPart, _ := pathpkg.Split(entry.Path); dirPart != "" {
			subdirs[pathpkg.Clean(dirPart)] = struct{}{}
		}
	}
	for subdir := range subdirs {
		full := filepath.Join(stagingDir, filepath.FromSlash(subdir))
		if err := removeEmptyDirEntry(full); err != nil {
			return fmt.Errorf("removing staging subdirectory %s: %w", subdir, err)
		}
	}
	manifestPath := filepath.Join(stagingDir, ManifestFilename)
	if err := removeLeafEntry(manifestPath); err != nil {
		return fmt.Errorf("removing manifest %s: %w", ManifestFilename, err)
	}
	return nil
}

// removeLeafEntry removes a single, non-directory staging entry -- see
// removeStagingContents's own docstring for why lstat-checking first,
// though already redundant with unlink(2)'s own symlink-safety, is still
// the more auditable choice here.
func removeLeafEntry(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if info.IsDir() {
		return fmt.Errorf("%s is unexpectedly a directory, refusing to remove it as a leaf entry", path)
	}
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// removeEmptyDirEntry removes the fixed "zigbee2mqtt" staging
// subdirectory once every file inside it has already been removed by
// removeLeafEntry above -- lstat-checked first (refuses anything that is
// not a real, non-symlink directory), then `os.Remove`, which for a
// directory calls `rmdir(2)`: it fails outright if anything unexpected
// still exists inside, rather than forcibly emptying it.
func removeEmptyDirEntry(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("%s is not a real directory, refusing to remove it", path)
	}
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
