package main

import (
	"errors"
	"os"
	"syscall"
)

var errNotRegular = errors.New("not a regular file")
var errHardLinked = errors.New("more than one hard link")

// openRegularNoFollow is the Go port of agent/safe_io.py's own
// lstat-then-O_NOFOLLOW-then-fstat discipline: lstat path first (never
// follows a symlink), refuse anything that is not a regular file there,
// then open with O_NOFOLLOW (a symlink swapped in during the narrow
// window between the lstat and this open fails the open outright,
// ELOOP/ENOTDIR, instead of being followed), then fstat the opened
// descriptor itself (not the path a second time, which would reopen the
// very race this is meant to close) and refuse again if that is somehow
// not a regular file either. Returns the open file (caller must Close)
// and the fstat-derived size.
func openRegularNoFollow(path string) (*os.File, int64, error) {
	lst, err := os.Lstat(path)
	if err != nil {
		return nil, 0, err
	}
	if lst.Mode()&os.ModeSymlink != 0 || !lst.Mode().IsRegular() {
		return nil, 0, errNotRegular
	}

	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, 0, err
	}
	info, err := file.Stat()
	if err != nil {
		file.Close()
		return nil, 0, err
	}
	if !info.Mode().IsRegular() {
		file.Close()
		return nil, 0, errNotRegular
	}
	return file, info.Size(), nil
}

// openStagedFileNoFollow additionally refuses a file with more than one
// hard link (cross-review finding, P5.5c): without this check, a manifest
// entry could name a file the agent (or a local attacker with write
// access to the staging directory, CLAUDE.md security principle 5's own
// "never trust the agent process" reasoning applied one step further)
// had hard-linked to some other, unrelated path -- this program would
// then read/verify one name for that inode while a second name for the
// very same inode could be mutated concurrently, or (in the previous,
// rename-based design this function's caller replaced) moved and
// chowned/chmoded *by path*, silently mutating whatever else that inode
// was linked from. `st_nlink == 1` is the only thing that proves this
// process is looking at the *only* path to this inode, checked via the
// already-open descriptor's own fstat (never the path a second time).
func openStagedFileNoFollow(path string) (*os.File, int64, error) {
	file, size, err := openRegularNoFollow(path)
	if err != nil {
		return nil, 0, err
	}
	info, err := file.Stat()
	if err != nil {
		file.Close()
		return nil, 0, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		file.Close()
		return nil, 0, errors.New("could not determine link count on this platform")
	}
	if stat.Nlink != 1 {
		file.Close()
		return nil, 0, errHardLinked
	}
	return file, size, nil
}

// lstatIsDirNoSymlink reports whether path exists, is a directory, and is
// not itself a symlink (lstat only, never follows) -- used for the
// staging directory itself, its fixed "zigbee2mqtt" subdirectory, and
// every destination directory a validated file is copied into, mirroring
// agent/restore.py::_assert_component_is_safe's own check on the writer
// side.
func lstatIsDirNoSymlink(path string) (bool, error) {
	info, err := os.Lstat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return false, nil
	}
	return info.IsDir(), nil
}
