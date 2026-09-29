package main

import (
	"errors"
	"os"
	"syscall"
)

var errNotRegular = errors.New("not a regular file")

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

// lstatIsDirNoSymlink reports whether path exists, is a directory, and is
// not itself a symlink (lstat only, never follows) -- used for the
// staging directory itself and its fixed "zigbee2mqtt" subdirectory,
// mirroring agent/restore.py::_assert_component_is_safe's own check on
// the writer side.
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
