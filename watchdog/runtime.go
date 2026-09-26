// The production Runtime (section 18.3): the container runtime is
// addressed purely through its command-line tool via os/exec, never a
// library. bin/container (main.go flags) are the only configurable
// pieces -- this is the one concrete pairing the specification's own
// examples use; a runtime that speaks a different command set entirely
// gets its own Runtime implementation, not more flags here.
package main

import (
	"os/exec"
	"strconv"
	"strings"
)

// cliRuntime is the concrete Runtime used outside of tests.
type cliRuntime struct {
	bin       string
	container string
}

// Start implements Runtime: drop a stopped container of the same name, if
// any (an earlier revision, or a previous failed start), then run digest
// under that name.
func (r cliRuntime) Start(digest string) error {
	_ = exec.Command(r.bin, "rm", "-f", r.container).Run()
	return exec.Command(r.bin, "run", "-d", "--name", r.container, digest).Run()
}

// Status implements Runtime via a single "inspect", the currently running
// image and the restart count in one call. A container that does not exist
// yet (first boot, or after Start's own "rm -f") is "not running", not an
// error -- there is simply nothing to report on yet.
func (r cliRuntime) Status() (running bool, digest string, restarts int, err error) {
	out, runErr := exec.Command(r.bin, "inspect", "-f", "{{.State.Running}} {{.Config.Image}} {{.RestartCount}}", r.container).Output()
	if runErr != nil {
		return false, "", 0, nil
	}
	fields := strings.Fields(string(out))
	if len(fields) < 3 || fields[0] != "true" {
		return false, "", 0, nil
	}
	restarts, _ = strconv.Atoi(fields[2])
	return true, fields[1], restarts, nil
}
