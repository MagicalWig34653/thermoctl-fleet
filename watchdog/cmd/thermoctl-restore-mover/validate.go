package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
)

// MaxStagedFileBytes bounds the size of any single staged data file this
// program will ever copy (cross-review finding, P5.5c) -- documented next
// to agent/restore.py's own side of this contract
// (agent/restore.py::apply_pending_restore's own docstring references
// this constant by name): operational-data backups stay "well within
// section 15.1's own 'a few megabytes' ceiling" per protocol/restore.py's
// own module docstring; 256 MiB is generous headroom above that, chosen
// so a legitimate backup is never rejected while an unbounded read is
// never attempted for a manifest entry that lies about its own size.
const MaxStagedFileBytes = 256 * 1024 * 1024

// preparedFile is one manifest entry that has been fully validated *and*
// copied into a freshly created, root-owned temporary file inside its
// real destination directory -- moveFiles (move.go) only ever renames
// files already in this state, never a path this program has not itself
// opened, hashed, and copied byte for byte.
//
// **Why copy instead of rename the staged file directly (the design this
// replaces, cross-review finding):** the staged file lives in a
// directory the agent process -- explicitly untrusted, CLAUDE.md security
// principle 5 -- can still write into for as long as this program is
// running. A `validate, then later os.Rename(srcPath, ...)` split (the
// previous design) reopens exactly the path the validation step itself
// closed: the agent can swap the staged path for a symlink, or a hard
// link to something outside staging, in the window between validation
// and the rename, and a *path-based* rename or chown/chmod afterward would
// follow that swap. Reading, hashing, and copying the bytes in **one
// continuous pass from the same open file descriptor** the safety checks
// themselves were performed on removes that window entirely: whatever this
// program ends up writing to the destination is provably the exact bytes
// it lstat-opened and fstat-checked, regardless of anything that happens
// to the path afterward.
type preparedFile struct {
	RelPath   string
	TempPath  string
	FinalPath string
	DestDir   string
}

// validateAll runs every check from this program's own top docstring
// (mover.go) in order, stopping at the first failure -- validation (and
// the copy into temporary destination-side files it now includes) never
// partially completes silently in a way that leaves stray temp files
// behind: on any failure, every temp file already created by an earlier,
// successful entry in this same call is removed before the error is
// returned (cleanupPrepared below) -- they are root-created, inside
// root-writable destination directories, safe for this program itself to
// remove; nothing under stagingDir is ever touched by this cleanup.
func validateAll(stagingDir string, manifest *Manifest, targets Targets) ([]preparedFile, error) {
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

	prepared := make([]preparedFile, 0, len(manifest.Files))
	for _, entry := range manifest.Files {
		one, err := prepareFile(stagingDir, entry, targets)
		if err != nil {
			cleanupPrepared(prepared)
			return nil, err
		}
		prepared = append(prepared, one)
	}
	return prepared, nil
}

// cleanupPrepared removes every already-created temporary file -- called
// whenever validateAll (or main.go's own caller) abandons a run after some
// files were already prepared. Best-effort: a removal failure here is not
// itself escalated (there is already a more specific error being reported
// by the caller), but every attempt is made regardless of an earlier one
// failing.
func cleanupPrepared(prepared []preparedFile) {
	for _, one := range prepared {
		os.Remove(one.TempPath)
	}
}

// validateStagingContentsMatchManifest walks the staging directory (top
// level, plus one level into "zigbee2mqtt" if that subdirectory is
// present) and refuses if it contains a single entry not accounted for by
// the manifest -- manifest.json itself is the only implicit exception.
// Without this check, a symlink or an extra, unlisted file planted in
// staging would simply be ignored by the per-file loop below rather than
// refused -- silently ignoring an unexpected entry is exactly the kind of
// thing CLAUDE.md security principle 5 says this program, not the agent,
// must catch.
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

// prepareFile validates one manifest entry and, if every check passes,
// copies it byte for byte into a freshly created temporary file inside
// its real destination directory -- see preparedFile's own docstring for
// why this is a copy, not a rename of the staged path.
//
// Order: open the staged file safely (openStagedFileNoFollow: lstat, no
// symlinks, regular file only, exactly one hard link) -> confirm its
// fstat-reported size matches the manifest -> lstat the destination
// directory (must be a real directory, not a symlink) -> create a unique
// temp file there (O_CREAT|O_EXCL|O_NOFOLLOW, mode 0600) -> copy while
// hashing, capped at MaxStagedFileBytes -> confirm the actual byte count
// and the resulting sha256 both match the manifest -> fsync the temp file
// -> chown/chmod it (fd-based, see applyDestinationOwnership below) ->
// close both descriptors. Every failure path removes the temp file it
// was in the middle of creating before returning.
func prepareFile(stagingDir string, entry ManifestFile, targets Targets) (preparedFile, error) {
	srcPath := filepath.Join(stagingDir, filepath.FromSlash(entry.Path))

	src, size, err := openStagedFileNoFollow(srcPath)
	if err != nil {
		if os.IsNotExist(err) {
			return preparedFile{}, errUnsafe(DetailFileMissing)
		}
		if err == errNotRegular {
			return preparedFile{}, errUnsafe(DetailNotRegularFile)
		}
		if err == errHardLinked {
			return preparedFile{}, errUnsafe(DetailHardLinkedFile)
		}
		return preparedFile{}, err
	}
	defer src.Close()

	if size != entry.SizeBytes {
		return preparedFile{}, errUnsafe(DetailSizeMismatch)
	}
	if size > MaxStagedFileBytes {
		return preparedFile{}, errUnsafe(DetailFileTooLarge)
	}

	dstPath, err := destinationFor(entry.Path, targets)
	if err != nil {
		return preparedFile{}, err
	}
	destDir := filepath.Dir(dstPath)
	destDirInfo, err := os.Lstat(destDir)
	if err != nil {
		if os.IsNotExist(err) {
			return preparedFile{}, errUnsafe(DetailDestinationMissing)
		}
		return preparedFile{}, err
	}
	if destDirInfo.Mode()&os.ModeSymlink != 0 || !destDirInfo.IsDir() {
		return preparedFile{}, errUnsafe(DetailUnsafeDestination)
	}

	temp, tempPath, err := createTempInDir(destDir)
	if err != nil {
		return preparedFile{}, err
	}

	hasher := sha256.New()
	written, err := io.Copy(io.MultiWriter(temp, hasher), io.LimitReader(src, MaxStagedFileBytes+1))
	if err != nil {
		temp.Close()
		os.Remove(tempPath)
		return preparedFile{}, err
	}
	if written > MaxStagedFileBytes || written != entry.SizeBytes {
		temp.Close()
		os.Remove(tempPath)
		return preparedFile{}, errUnsafe(DetailSizeMismatch)
	}
	digest := hex.EncodeToString(hasher.Sum(nil))
	if digest != entry.SHA256 {
		temp.Close()
		os.Remove(tempPath)
		return preparedFile{}, errUnsafe(DetailHashMismatch)
	}

	if err := temp.Sync(); err != nil {
		temp.Close()
		os.Remove(tempPath)
		return preparedFile{}, err
	}
	if err := applyDestinationOwnership(temp, destDir); err != nil {
		temp.Close()
		os.Remove(tempPath)
		return preparedFile{}, err
	}
	if err := temp.Close(); err != nil {
		os.Remove(tempPath)
		return preparedFile{}, err
	}

	return preparedFile{RelPath: entry.Path, TempPath: tempPath, FinalPath: dstPath, DestDir: destDir}, nil
}

// createTempInDir creates a new, exclusively-owned regular file inside
// dir with a random, unpredictable name (O_CREAT|O_EXCL|O_NOFOLLOW, mode
// 0600) -- retried a handful of times against an astronomically unlikely
// name collision, never against an existing file (O_EXCL refuses to
// follow or truncate one). dir itself has already been confirmed a real,
// non-symlink directory by the caller.
func createTempInDir(dir string) (*os.File, string, error) {
	var lastErr error
	for attempt := 0; attempt < 8; attempt++ {
		suffix, err := randomHex(16)
		if err != nil {
			return nil, "", err
		}
		path := filepath.Join(dir, fmt.Sprintf(".thermoctl-restore-mover-%s.tmp", suffix))
		file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY|syscall.O_NOFOLLOW, 0o600)
		if err == nil {
			return file, path, nil
		}
		if !os.IsExist(err) {
			return nil, "", err
		}
		lastErr = err
	}
	return nil, "", fmt.Errorf("could not create a unique temporary file in %s: %w", dir, lastErr)
}

func randomHex(n int) (string, error) {
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}

// destinationFor maps a manifest's (already-allowlisted) relative path to
// its real, live destination -- the one place in this program that names
// the live paths a validated file actually gets copied to. Only ever
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

// applyDestinationOwnership decides ownership/mode for a just-copied
// file's already-open descriptor from the existing image recipe
// (image/common/agent-compose.yml, image/common/tmpfiles.d
// /thermoctl-agent.conf): neither one yet defines a compose file or a
// fixed uid/gid for the thermoctl/Zigbee2MQTT containers themselves
// (tracked as an open point in docs/STATUS.md's P5.4/P5.6 sections --
// those containers are reconciled by the agent, not shipped by this
// image, and their own uid/gid is not fixed anywhere in this repository
// yet). Hard-coding a numeric uid here would therefore be a guess this
// program has no way to verify against reality.
//
// The one signal this program *can* verify is the destination
// directory's own existing ownership -- whatever process created
// thermoctl_db_path's parent / zigbee2mqtt_dir already set it up for
// whichever uid actually needs to read it, the same reasoning
// image/common/tmpfiles.d/thermoctl-agent.conf already applies to
// /run/thermoctl-agent (pinned to the agent's own numeric uid/gid, not a
// name that only exists in a container's own /etc/passwd). This function
// therefore chowns the moved file to match its destination directory's
// owner/group, and sets mode 0644 (owner read/write, group and other
// read-only -- a data file the owning container's process needs to read
// and, for thermoctl.db, write, never execute).
//
// **fd-based (file.Chown/file.Chmod, i.e. fchown(2)/fchmod(2)), never
// by path** -- the same TOCTOU reasoning this whole redesign is built
// around: the file is already fully written and closed to nothing else,
// referenced only by this open descriptor, so there is no path-based
// race left to have.
func applyDestinationOwnership(file *os.File, destDir string) error {
	dirInfo, err := os.Lstat(destDir)
	if err != nil {
		return err
	}
	stat, ok := dirInfo.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("could not determine %s's owner on this platform", destDir)
	}
	if err := file.Chown(int(stat.Uid), int(stat.Gid)); err != nil {
		return err
	}
	return file.Chmod(0o644)
}
