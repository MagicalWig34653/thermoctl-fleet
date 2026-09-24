module github.com/magicalwig34653/thermoctl-fleet/watchdog

// go.mod without a single dependency -- an explicit condition from
// docs/specification.md section 18.3. In particular no Docker SDK: the
// container runtime is addressed via its command-line tool or via
// systemctl, not via a library.
go 1.23
