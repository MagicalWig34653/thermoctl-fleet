package main

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"syscall"
)

// validatedFile is one manifest entry that has passed every check --
// moveFiles only ever operates on a list of these, never on the manifest
// directly, so a move can never happen for a file this program has not
// itself already opened, hashed, and matched.
type validatedFile struct {
	RelPath string
	SrcPath string
	DstPath string
}

// validateAll runs every check from this program's own top docstring
// (mover.go) in order, stopping at the first failure -- validation never
// partially completes silently; the first violation found is reported as
// this whole operation's outcome. Returns the full list of validated
// files only if every single one of them, and the staging directory
// itself, passed.
func validateAll(stagingDir string, manifest *Manifest, targets Targets) ([]validatedFile, error) {
	if err := validateManifestShape(manifest); err != nil {
		return nil, err
	}

	isDir, err := lstatIsDirNoSymlink(stagingDir)
	if err != nil {
		return nil, err
	}
	if !isDir {
		return nil, errUnsafe(DetailUnsafeStaging)
	}

	if err := validateStagingContentsMatchManifest(stagingDir, manifest); err != nil {
		return nil, err
	}

	files := make([]validatedFile, 0, len(manifest.Files))
	for _, entry := range manifest.Files {
		validated, err := validateOneFile(stagingDir, entry, targets)
		if err != nil {
			return nil, err
		}
		files = append(files, validated)
	}
	return files, nil
}

// validateStagingContentsMatchManifest walks the staging directory (top
// level, plus one level into "zigbee2mqtt" if that subdirectory is
// present) and refuses if it contains a single entry not accounted for by
// the manifest -- manifest.json itself and this program's own temporary
// status-write files (never written inside stagingDir, see status.go) are
// the only implicit exceptions. Without this check, a symlink or an
// extra, unlisted file planted in staging would simply be ignored by the
// per-file loop below rather than refused -- silently ignoring an
// unexpected entry is exactly the kind of thing CLAUDE.md security
// principle 5 says this program, not the agent, must catch.
func validateStagingContentsMatchManifest(stagingDir string, manifest *Manifest) error {
	topLevelAllowed := map[string]struct{}{ManifestFilename: {}}
	subdirAllowed := map[string]struct{}{}
	for _, entry := range manifest.Files {
		dir, base := filepath.Split(entry.Path)
		if dir == "" {
			topLevelAllowed[base] = struct{}{}
			continue
		}
		topLevelAllowed[filepath.Clean(dir)] = struct{}{}
		subdirAllowed[entry.Path] = struct{}{}
	}

	if err := checkDirEntriesAllowed(stagingDir, topLevelAllowed); err != nil {
		return err
	}
	for subdir := range subdirAllowedTopDirs(subdirAllowed) {
		full := filepath.Join(stagingDir, subdir)
		isDir, err := lstatIsDirNoSymlink(full)
		if err != nil {
			return err
		}
		if !isDir {
			// Listed in the manifest (a file lives under it) but not a
			// real directory in staging -- caught here rather than left
			// to the per-file open below, so the detail is the more
			// specific "unexpected entry"/"unsafe staging" rather than a
			// confusing "file missing".
			return errUnsafe(DetailUnsafeStaging)
		}
		allowedBases := map[string]struct{}{}
		for rel := range subdirAllowed {
			if filepath.Dir(rel) == subdir {
				allowedBases[filepath.Base(rel)] = struct{}{}
			}
		}
		if err := checkDirEntriesAllowed(full, allowedBases); err != nil {
			return err
		}
	}
	return nil
}

func subdirAllowedTopDirs(subdirAllowed map[string]struct{}) map[string]struct{} {
	dirs := map[string]struct{}{}
	for rel := range subdirAllowed {
		dirs[filepath.Dir(rel)] = struct{}{}
	}
	return dirs
}

func checkDirEntriesAllowed(dir string, allowed map[string]struct{}) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if _, ok := allowed[entry.Name()]; !ok {
			return errUnsafe(DetailUnexpectedEntry)
		}
	}
	return nil
}

// validateOneFile validates a single manifest entry: opens it safely
// (openRegularNoFollow, never following a symlink), checks its size and
// sha256 -- both computed from the already-open descriptor, never a
// second, separate stat/open of the path -- against the manifest, and
// resolves + checks its destination.
func validateOneFile(stagingDir string, entry ManifestFile, targets Targets) (validatedFile, error) {
	srcPath := filepath.Join(stagingDir, filepath.FromSlash(entry.Path))

	file, size, err := openRegularNoFollow(srcPath)
	if err != nil {
		if os.IsNotExist(err) {
			return validatedFile{}, errUnsafe(DetailFileMissing)
		}
		if err == errNotRegular {
			return validatedFile{}, errUnsafe(DetailNotRegularFile)
		}
		return validatedFile{}, err
	}
	defer file.Close()

	if size != entry.SizeBytes {
		return validatedFile{}, errUnsafe(DetailSizeMismatch)
	}

	hasher := sha256.New()
	if _, err := io.Copy(hasher, file); err != nil {
		return validatedFile{}, err
	}
	digest := hex.EncodeToString(hasher.Sum(nil))
	if digest != entry.SHA256 {
		return validatedFile{}, errUnsafe(DetailHashMismatch)
	}

	dstPath, err := destinationFor(entry.Path, targets)
	if err != nil {
		return validatedFile{}, err
	}
	dstDir := filepath.Dir(dstPath)
	dstDirInfo, err := os.Stat(dstDir)
	if err != nil {
		if os.IsNotExist(err) {
			return validatedFile{}, errUnsafe(DetailDestinationMissing)
		}
		return validatedFile{}, err
	}
	if !dstDirInfo.IsDir() {
		return validatedFile{}, errUnsafe(DetailDestinationMissing)
	}

	stagingInfo, err := os.Stat(stagingDir)
	if err != nil {
		return validatedFile{}, err
	}
	if !sameDevice(stagingInfo, dstDirInfo) {
		return validatedFile{}, errUnsafe(DetailCrossFilesystem)
	}

	return validatedFile{RelPath: entry.Path, SrcPath: srcPath, DstPath: dstPath}, nil
}

// destinationFor maps a manifest's (already-allowlisted) relative path to
// its real, live destination -- the one place in this program that names
// the live paths a validated file actually gets moved to. Only ever
// called after allowedManifestPaths has already confirmed entry.Path is
// one of the three known values; the default case is defense in depth,
// unreachable via any real path through validateAll.
func destinationFor(relPath string, targets Targets) (string, error) {
	switch relPath {
	case "thermoctl.db":
		return targets.ThermoctlDBPath, nil
	case "zigbee2mqtt/database.db":
		return filepath.Join(targets.Zigbee2mqttDir, "database.db"), nil
	case "zigbee2mqtt/coordinator_backup.json":
		return filepath.Join(targets.Zigbee2mqttDir, "coordinator_backup.json"), nil
	default:
		return "", errUnsafe(DetailUnknownFileName)
	}
}

// sameDevice reports whether a and b's os.FileInfo (from os.Stat) name
// locations on the same filesystem/device -- the pre-move check backing
// "staging and destination must be on the same filesystem, refuse on
// EXDEV rather than copy". Uses the platform *syscall.Stat_t (stdlib
// only, no dependency) rather than comparing paths textually, which would
// say nothing about actual mount boundaries (e.g. a bind mount at a
// different path, the same device).
func sameDevice(a, b os.FileInfo) bool {
	aStat, aOK := a.Sys().(*syscall.Stat_t)
	bStat, bOK := b.Sys().(*syscall.Stat_t)
	if !aOK || !bOK {
		// Unknown platform for this stdlib type assertion -- fail closed
		// (never move) rather than assume "same" without proof.
		return false
	}
	return uint64(aStat.Dev) == uint64(bStat.Dev)
}
