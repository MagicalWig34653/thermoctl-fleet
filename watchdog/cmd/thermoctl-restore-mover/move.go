package main

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// moveResult is what moveFiles reports back to main.go: how many of the
// validated files were actually moved (for the log line only -- the
// status file itself only ever carries one of the closed DETAIL_*
// strings, never a count) and whether every one of them made it.
type moveResult struct {
	Moved int
	Total int
}

func (r moveResult) complete() bool { return r.Moved == r.Total }

// moveFiles moves every already-validated file into place, in order, via
// os.Rename -- same-filesystem, atomic per file, never copy-then-delete
// (validateAll's own sameDevice check already refused anything that is
// not on the same filesystem as its destination; this function additionally
// refuses to fall back to a copy if Rename itself still reports EXDEV,
// e.g. because something changed the mount layout between validation and
// this call).
//
// **Stops at the first failure and returns immediately** -- whatever was
// already moved stays moved (each os.Rename is already atomic and
// complete by the time it returns success), and whatever was not yet
// moved stays in staging, untouched, for a later run to finish once the
// underlying problem is fixed. This function never deletes anything in
// staging itself; the caller (main.go) removes the whole staging
// directory only once moveResult.complete() is true.
func moveFiles(files []validatedFile, warn func(format string, args ...any)) moveResult {
	result := moveResult{Total: len(files)}
	for _, file := range files {
		if err := os.Rename(file.SrcPath, file.DstPath); err != nil {
			if isExdev(err) {
				warn("thermoctl-restore-mover: %s and %s are not on the same filesystem after all (EXDEV) -- refusing to copy, leaving it staged", file.SrcPath, file.DstPath)
			} else {
				warn("thermoctl-restore-mover: moving %s to %s failed: %v", file.SrcPath, file.DstPath, err)
			}
			return result
		}
		result.Moved++

		applyDestinationOwnership(file.DstPath, warn)
	}
	return result
}

func isExdev(err error) bool {
	linkErr, ok := err.(*os.LinkError)
	if !ok {
		return false
	}
	errno, ok := linkErr.Err.(syscall.Errno)
	return ok && errno == syscall.EXDEV
}

// applyDestinationOwnership decides ownership/mode for a just-moved file
// from the existing image recipe (image/common/agent-compose.yml,
// image/common/tmpfiles.d/thermoctl-agent.conf): neither one yet defines
// a compose file or a fixed uid/gid for the thermoctl/Zigbee2MQTT
// containers themselves (tracked as an open point in docs/STATUS.md's
// P5.4/P5.6 sections -- those containers are reconciled by the agent, not
// shipped by this image, and their own uid/gid is not fixed anywhere in
// this repository yet). Hard-coding a numeric uid here would therefore be
// a guess this program has no way to verify against reality.
//
// The one signal this program *can* verify is the destination directory's
// own existing ownership -- whatever process created
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
// **Best-effort, not fatal to the overall move.** A chown/chmod failure
// here (e.g. this process somehow not running as root, or an unusual
// filesystem) does not roll back or fail the already-completed rename --
// the data is safely in its destination either way, and a landlord/tenant
// having to fix a permission bit by hand is a far smaller problem than
// this program leaving a successfully-moved restore in limbo or reporting
// "applied" as a lie. Logged, never silently swallowed.
func applyDestinationOwnership(path string, warn func(format string, args ...any)) {
	dirInfo, err := os.Stat(filepath.Dir(path))
	if err != nil {
		warn("thermoctl-restore-mover: could not stat %s's destination directory to match its ownership: %v", path, err)
		return
	}
	stat, ok := dirInfo.Sys().(*syscall.Stat_t)
	if !ok {
		warn("thermoctl-restore-mover: could not determine %s's destination directory's owner on this platform", path)
		return
	}
	if err := os.Chown(path, int(stat.Uid), int(stat.Gid)); err != nil {
		warn("thermoctl-restore-mover: chown %s to %d:%d failed: %v", path, stat.Uid, stat.Gid, err)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		warn("thermoctl-restore-mover: chmod %s failed: %v", path, err)
	}
}

// removeStagingDir removes the whole staging directory, manifest
// included -- called only once moveResult.complete() is true, so a later
// restore can stage again (agent/restore.py::_staged_restore_already_pending
// checks exactly this directory's manifest for its own "already staged"
// refusal).
func removeStagingDir(stagingDir string) error {
	if err := os.RemoveAll(stagingDir); err != nil {
		return fmt.Errorf("removing staging directory %s: %w", stagingDir, err)
	}
	return nil
}
