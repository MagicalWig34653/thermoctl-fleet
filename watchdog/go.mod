module github.com/magicalwig34653/thermoctl-fleet/watchdog

// go.mod without a single dependency -- an explicit condition from
// docs/specification.md section 18.3. In particular no Docker SDK: the
// container runtime is addressed via its command-line tool or via
// systemctl, not via a library.
//
// "go 1.24" (bumped from 1.23, P5.5d cross-review fix) is a toolchain
// version requirement, not a dependency -- this line names zero
// `require`s either way. Needed for `os.Root` (stdlib, added in Go
// 1.24), which cmd/thermoctl-restore-mover/stagingroot.go uses to
// confine every filesystem operation against the staging directory to
// that directory's own already-open root, closing an intermediate-
// path-component symlink gap a plain path join could not.
go 1.24
