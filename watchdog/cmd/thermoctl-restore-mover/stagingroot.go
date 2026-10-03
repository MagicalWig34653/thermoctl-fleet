package main

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	pathpkg "path"
	"syscall"
)

// StagingRoot (P5.5d cross-review fix) confines every filesystem
// operation this program performs against the staging directory to a
// single, already-open os.Root -- opened exactly once per run() and
// reused across initial validation, the resume decision's re-validation,
// and final cleanup, so a later swap of any path component (the staging
// directory's own name in its parent, or its fixed "zigbee2mqtt"
// subdirectory's own name) cannot redirect an operation that was already
// using an earlier, safe resolution.
//
// **The gap this closes (cross-review finding after this package's own
// first commit).** The previous design built plain, multi-component
// relative paths such as filepath.Join(stagingDir, "zigbee2mqtt/database.db")
// and Lstat/Open/Removed them as one joined string. Neither a bare Lstat
// nor O_NOFOLLOW protects anything but the *final* path component --
// "zigbee2mqtt" itself, an *intermediate* component, was silently
// resolved through if the agent (untrusted, CLAUDE.md security principle
// 5) swapped it for a symlink between this program's early checks and a
// later access. Demonstrated by cross-review:
//   - a decoy file elsewhere, with a hash matching the manifest, reached
//     through a swapped "zigbee2mqtt" symlink, would be read (hashed,
//     copied) as if it were the real staged file, without this program
//     ever opening anything actually inside the real staging directory;
//   - after a successful move, swapping "zigbee2mqtt" (or the staging
//     directory's own entry in *its* parent) for a symlink into the
//     *live* directory just populated would make this root-running
//     program's own cleanup step delete the live file it had just
//     restored.
//
// **os.Root's own documented limit, closed explicitly here, never relied
// on implicitly.** Root refuses anything that would resolve *outside*
// the directory it was opened for (an absolute symlink, a ".." escape, a
// relative symlink whose target lies outside) -- verified empirically
// against a real Linux build while designing this fix. It does **not**
// refuse a relative symlink that stays *inside* the root (also verified
// empirically: `root.Open` on such a symlink follows it and returns the
// *other* file's content), and passing `syscall.O_NOFOLLOW` to
// `Root.OpenFile` does **not** change that (Root silently does not honor
// it). This program refuses ALL symlinks under staging, not only ones
// that would escape it, so every entry -- and the fixed "zigbee2mqtt"
// subdirectory itself -- is `Lstat`-ed through the appropriate `*os.Root`
// and refused if it is a symlink, *before* any Open/Remove call ever
// reaches it. The Lstat-then-act pair this still leaves is the narrowest
// residual the public `os.Root` API allows (there is no atomic "open
// only if not a symlink" primitive for a name inside a Root the way
// O_NOFOLLOW gives one for a plain file) -- the same class of residual
// this program already accepts elsewhere (e.g. `prepareFile`'s own
// destination-directory Lstat immediately before creating a file inside
// it).
//
// **Why "zigbee2mqtt" needs its own, separately-held sub-Root, not just
// a one-time Lstat check.** Root's own path resolution handles a
// multi-component name like "zigbee2mqtt/database.db" safely with
// respect to *escaping* the root on every call, but each such call
// independently re-resolves "zigbee2mqtt" from the top root's own held
// directory handle -- an Lstat performed once, followed later by a
// *separate* multi-component Open/Remove call, reopens exactly the same
// window the original bug had, only narrower. Opening "zigbee2mqtt"
// exactly once (immediately after Lstat-confirming it is a real,
// non-symlink directory) as its own `os.Root`, and performing every
// later single-component operation on its two children through *that*
// held sub-root, removes the re-resolution entirely: "zigbee2mqtt" is
// never looked up by name a second time within the same run.
//
// Built on `os.Root` (Go 1.24+, stdlib only -- watchdog/go.mod's own "go"
// directive bumped from 1.23 to 1.24 for this; still zero `require`
// lines, a toolchain-version requirement, not a dependency).
type StagingRoot struct {
	root   *os.Root
	zigbee *os.Root // nil until zigbeeRoot succeeds once; stays nil if absent
}

// zigbeeDirName is the one fixed subdirectory name this whole program
// ever descends into -- named once here so every reference to it (the
// top-level allowlist entry, the sub-root's own name, the final removal)
// uses the identical literal.
const zigbeeDirName = "zigbee2mqtt"

// openStagingRoot opens stagingDir itself as an os.Root. Returns (nil,
// nil) if stagingDir does not exist at all -- mirrors parseManifest's own
// "absent, not an error, nothing pending" case, since a staged restore
// cannot exist without its own staging directory. Returns a
// DetailUnsafeStaging-detailed error if stagingDir exists but is not a
// real, non-symlink directory (`lstatIsDirNoSymlink`, the same check
// every other caller in this program already uses for a directory it is
// about to trust).
func openStagingRoot(stagingDir string) (*StagingRoot, error) {
	isDir, err := lstatIsDirNoSymlink(stagingDir)
	if err != nil {
		return nil, err
	}
	if !isDir {
		if _, statErr := os.Lstat(stagingDir); os.IsNotExist(statErr) {
			return nil, nil
		}
		return nil, errUnsafe(DetailUnsafeStaging)
	}
	root, err := os.OpenRoot(stagingDir)
	if err != nil {
		return nil, err
	}
	return &StagingRoot{root: root}, nil
}

// Close releases both held roots (the top-level staging root and, if it
// was ever opened, the "zigbee2mqtt" sub-root).
func (sr *StagingRoot) Close() error {
	var err error
	if sr.zigbee != nil {
		err = sr.zigbee.Close()
	}
	if cerr := sr.root.Close(); err == nil {
		err = cerr
	}
	return err
}

// zigbeeRoot returns the held sub-root for the "zigbee2mqtt"
// subdirectory, opening it -- Lstat-checked, exactly once per StagingRoot
// -- on first use, and caching it for the remainder of this StagingRoot's
// lifetime. present is false if "zigbee2mqtt" does not exist at all --
// not every staged backup includes Zigbee2MQTT data.
func (sr *StagingRoot) zigbeeRoot() (root *os.Root, present bool, err error) {
	if sr.zigbee != nil {
		return sr.zigbee, true, nil
	}
	info, err := sr.root.Lstat(zigbeeDirName)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, false, nil
		}
		return nil, false, err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return nil, false, errUnsafe(DetailUnsafeStaging)
	}
	sub, err := sr.root.OpenRoot(zigbeeDirName)
	if err != nil {
		return nil, false, err
	}
	sr.zigbee = sub
	return sub, true, nil
}

// resolveEntry maps one of the manifest's own relative paths onto the
// (root, name) pair every later operation on it should use -- the one
// place that decides whether an entry is a top-level file (resolved
// against the top root) or lives under "zigbee2mqtt" (resolved against
// the held sub-root, opened via zigbeeRoot above). present is false only
// when the caller asked for a "zigbee2mqtt/..." entry and "zigbee2mqtt"
// itself does not exist at all -- this function only reports what it
// found, the caller decides whether that is acceptable.
func (sr *StagingRoot) resolveEntry(relPath string) (root *os.Root, name string, present bool, err error) {
	dir, base := pathpkg.Split(relPath)
	if dir == "" {
		return sr.root, base, true, nil
	}
	sub, present, err := sr.zigbeeRoot()
	if err != nil || !present {
		return nil, "", present, err
	}
	return sub, base, true, nil
}

// lstatEntryNoFollow Lstats one manifest entry through the correct root
// and refuses anything that is not a lone-linked regular file --
// errNotRegular/errHardLinked (safeopen.go) are reused here so callers
// can share openStagedFileNoFollow's own error handling. The full
// os.FileInfo is returned too (not only the size), so a caller that later
// opens the same name can compare the two with os.SameFile -- see
// openEntryNoFollow below for why that comparison matters.
func (sr *StagingRoot) lstatEntryNoFollow(relPath string) (root *os.Root, name string, info os.FileInfo, err error) {
	root, name, present, err := sr.resolveEntry(relPath)
	if err != nil {
		return nil, "", nil, err
	}
	if !present {
		return nil, "", nil, os.ErrNotExist
	}
	info, err = root.Lstat(name)
	if err != nil {
		return nil, "", nil, err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return nil, "", nil, errNotRegular
	}
	if err := requireSingleLink(info); err != nil {
		return nil, "", nil, err
	}
	return root, name, info, nil
}

// requireSingleLink extracts the platform link count from info and
// refuses anything but exactly one hard link -- shared between the
// pre-open Lstat check and the post-open Fstat re-check below, so both
// apply the identical rule.
func requireSingleLink(info os.FileInfo) error {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return errors.New("could not determine link count on this platform")
	}
	if stat.Nlink != 1 {
		return errHardLinked
	}
	return nil
}

// testPostLstatHook, when non-nil, runs immediately after an Lstat and
// immediately before the matching root.OpenFile call -- in both
// openEntryNoFollow (below) and readManifestBytes (further down), the two
// read paths in this file that go through os.Root and therefore cannot
// rely on O_NOFOLLOW. Test-only (set in stagingroot_test.go), used to
// deterministically reproduce the race an attacker would otherwise need
// precise timing for: swapping the entry for an in-root symlink (or a
// second hard link) in the narrow window between the two calls. Left nil
// in production, where it costs nothing.
var testPostLstatHook func()

// openEntryNoFollow Lstat-checks (lstatEntryNoFollow above), then opens,
// one manifest entry for reading -- the read-side counterpart of
// prepareFile's own validated-open, routed through the held root(s) so
// neither "zigbee2mqtt" nor the staging directory itself is ever
// re-resolved by name partway through a run.
//
// **Post-open re-check (P5.5d cross-review follow-up, docs/STATUS.md).**
// os.Root's own OpenFile does not honor O_NOFOLLOW (stagingroot.go's own
// top docstring) and, unlike a plain os.OpenFile on a real path, has no
// equivalent of the kernel refusing to open a symlink outright -- an
// entry Lstat-confirmed to be a lone-linked regular file can still be
// swapped for an in-root symlink (which Root silently follows) in the
// window between that Lstat and this Open. Closed the same way
// openRegularNoFollow already closes the analogous window on a plain
// path: Fstat the *returned descriptor* (never re-Lstat the name, which
// would just re-open the identical race one call later) and refuse
// unless it is still a lone-linked regular file *and* os.SameFile against
// the original Lstat's info holds -- the latter is what actually catches
// a swap-for-a-same-shaped-decoy that an Fstat-only check would miss (a
// symlink target that happens to also be a lone-linked regular file would
// otherwise pass the type/link-count checks alone).
func (sr *StagingRoot) openEntryNoFollow(relPath string) (*os.File, int64, error) {
	root, name, lst, err := sr.lstatEntryNoFollow(relPath)
	if err != nil {
		return nil, 0, err
	}
	if testPostLstatHook != nil {
		testPostLstatHook()
	}
	file, err := root.OpenFile(name, os.O_RDONLY, 0)
	if err != nil {
		return nil, 0, err
	}
	fstat, err := file.Stat()
	if err != nil {
		file.Close()
		return nil, 0, err
	}
	if fstat.Mode()&os.ModeSymlink != 0 || !fstat.Mode().IsRegular() {
		file.Close()
		return nil, 0, errNotRegular
	}
	if err := requireSingleLink(fstat); err != nil {
		file.Close()
		return nil, 0, err
	}
	if !os.SameFile(lst, fstat) {
		file.Close()
		return nil, 0, errUnsafe(DetailUnsafeStaging)
	}
	return file, fstat.Size(), nil
}

// topLevelNames lists every entry directly inside the staging directory
// -- used by validateStagingContentsMatchManifest's "every entry actually
// present is accounted for" check.
func (sr *StagingRoot) topLevelNames() ([]string, error) {
	entries, err := fs.ReadDir(sr.root.FS(), ".")
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	return names, nil
}

// zigbeeNames lists every entry inside the "zigbee2mqtt" sub-root --
// present mirrors zigbeeRoot's own meaning (false if the subdirectory
// does not exist at all).
func (sr *StagingRoot) zigbeeNames() (names []string, present bool, err error) {
	sub, present, err := sr.zigbeeRoot()
	if err != nil || !present {
		return nil, present, err
	}
	entries, err := fs.ReadDir(sub.FS(), ".")
	if err != nil {
		return nil, true, err
	}
	names = make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	return names, true, nil
}

// removeEntry Lstat-checks then removes one manifest entry (a regular
// file) through the correct root, aborting -- leaving it in place --
// rather than removing anything if it unexpectedly turns out not to be a
// regular file (the cleanup step's own "abort on the first unexpected
// entry type" contract). POSIX unlink(2) never dereferences the final
// named component being removed regardless, so this Lstat is defense in
// depth on an already-safe leaf removal, not the only thing making it
// safe.
func (sr *StagingRoot) removeEntry(relPath string) error {
	root, name, present, err := sr.resolveEntry(relPath)
	if err != nil {
		return err
	}
	if !present {
		return nil
	}
	info, err := root.Lstat(name)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if info.IsDir() {
		return fmt.Errorf("%s is unexpectedly a directory, refusing to remove it as a leaf entry", relPath)
	}
	if err := root.Remove(name); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// removeZigbeeDirIfPresent removes the "zigbee2mqtt" subdirectory itself
// through the TOP root -- a single-component removal by construction
// (POSIX rmdir(2) never dereferences its own, final named component
// either), so it needs no held sub-root of its own; called only once
// every file inside it has already been removed via removeEntry above
// (through the held sub-root, so those removals are immune to
// "zigbee2mqtt" being renamed/symlinked in between). Refuses -- without
// removing anything -- if the entry is not a real, non-symlink directory.
func (sr *StagingRoot) removeZigbeeDirIfPresent() error {
	info, err := sr.root.Lstat(zigbeeDirName)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("%s is not a real directory, refusing to remove it", zigbeeDirName)
	}
	if err := sr.root.Remove(zigbeeDirName); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// removeManifest removes manifest.json itself through the top root --
// also a single-component removal, safe by the same construction as
// removeZigbeeDirIfPresent above.
func (sr *StagingRoot) removeManifest() error {
	info, err := sr.root.Lstat(ManifestFilename)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if info.IsDir() {
		return fmt.Errorf("%s is unexpectedly a directory, refusing to remove it", ManifestFilename)
	}
	if err := sr.root.Remove(ManifestFilename); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// readManifestBytes reads manifest.json through the top root -- Lstat
// checked (symlink/non-regular refused) and capped at MaxManifestBytes
// via io.LimitReader, exactly like the rest of this program's staged-file
// reads. present is false only if manifest.json does not exist at all.
//
// **Post-open re-check (P5.5d cross-review follow-up)**: the same gap
// openEntryNoFollow closes above applies here verbatim -- manifest.json
// is itself agent-controlled and read through the same os.Root, which
// does not honor O_NOFOLLOW and silently follows an in-root symlink. The
// Fstat/Nlink/os.SameFile re-check on the opened descriptor is identical.
func (sr *StagingRoot) readManifestBytes() (data []byte, present bool, err error) {
	lst, err := sr.root.Lstat(ManifestFilename)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, false, nil
		}
		return nil, true, err
	}
	if lst.Mode()&os.ModeSymlink != 0 || !lst.Mode().IsRegular() {
		return nil, true, errNotRegular
	}
	if err := requireSingleLink(lst); err != nil {
		return nil, true, err
	}
	if testPostLstatHook != nil {
		testPostLstatHook()
	}
	file, err := sr.root.OpenFile(ManifestFilename, os.O_RDONLY, 0)
	if err != nil {
		return nil, true, err
	}
	defer file.Close()

	fstat, err := file.Stat()
	if err != nil {
		return nil, true, err
	}
	if fstat.Mode()&os.ModeSymlink != 0 || !fstat.Mode().IsRegular() {
		return nil, true, errNotRegular
	}
	if err := requireSingleLink(fstat); err != nil {
		return nil, true, err
	}
	if !os.SameFile(lst, fstat) {
		return nil, true, errUnsafe(DetailUnsafeStaging)
	}

	data, err = io.ReadAll(io.LimitReader(file, MaxManifestBytes+1))
	if err != nil {
		return nil, true, err
	}
	if len(data) > MaxManifestBytes {
		return nil, true, errManifestTooLarge
	}
	return data, true, nil
}
