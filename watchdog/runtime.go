// The production Runtime (section 18.3): the container runtime is
// addressed purely through its command-line tool via os/exec, never a
// library. bin/compose/repo (main.go flags) are the only configurable
// pieces -- this is the one concrete pairing the specification's own
// examples use; a runtime that speaks a different command set entirely
// gets its own Runtime implementation, not more flags here.
//
// Start never runs the digest directly (cross-review R1/R5): a bare
// "docker run <digest>" has no restart policy (so Docker never counts
// restarts, and "three restarts in a row" from section 17 step 5 could
// never trigger) and no volumes for the agent to actually do its job. The
// agent's real run configuration -- restart policy, the state/health file
// mounts, the Docker socket it needs to manage the other three services
// (section 13) -- instead lives in a fixed compose file shipped with the
// system image (image/common/agent-compose.yml, never from the cloud: a
// *fixed*, locally shipped file is not the "arbitrary compose files" section
// 13 forbids the cloud from sending). Start only (a) locally tags digest as
// the fixed name that file references, no network, and (b) re-applies that
// file with --pull never (belt and suspenders with the file's own
// pull_policy: never, R1) so a missing image is refused, not fetched.
//
// desired/proven in the state file are the same *registry manifest*
// digests section 13's desired state carries -- not a local image ID. A
// bare "docker tag sha256:<manifest digest> ..." does not resolve at all
// (main-session finding, additional to R1/R5): only "<repo>@sha256:..."
// does, and only once the agent has pulled that repo by digest so the
// image carries it as one of its RepoDigests. r.repo is therefore the
// agent's own hard-coded source (security principle 2) -- not the cloud's
// to name, only ever a flag default the operator confirms matches
// agent/'s source list.
package main

import (
	"fmt"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
)

// digestPattern is the only shape a digest may have before it reaches argv
// (cross-review R4): otherwise a value starting with "-" would be parsed as
// a command-line option, not an image reference.
var digestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// execCommand stands in for exec.Command so tests can record the argv
// Start/Status build (cross-review R3) -- in particular that --pull never
// is present and that an invalid digest never reaches it at all -- without
// ever touching Docker.
var execCommand = exec.Command

// agentContainerName is fixed by the compose file's own container_name
// (image/common/agent-compose.yml), not a flag: the two would only have to
// be kept in sync by hand otherwise -- one name, one place.
const agentContainerName = "thermoctl-agent"

// cliRuntime is the concrete Runtime used outside of tests.
type cliRuntime struct {
	bin     string
	compose string
	repo    string
}

// Start implements Runtime: see the module doc comment for why this is a
// local tag plus a fixed compose file, not a bare "docker run <digest>".
func (r cliRuntime) Start(digest string) error {
	if !digestPattern.MatchString(digest) {
		return fmt.Errorf("refusing to start %q: not a plain sha256 digest", digest)
	}
	if err := execCommand(r.bin, "tag", r.repo+"@"+digest, agentContainerName+":current").Run(); err != nil {
		return fmt.Errorf("tagging %q: %w", r.repo+"@"+digest, err)
	}
	return execCommand(r.bin, "compose", "-f", r.compose, "up", "-d", "--pull", "never", "--force-recreate", "agent").Run()
}

// Status implements Runtime via a single "inspect" for the running state,
// the restart count, and the container's local image ID, plus one more to
// resolve that ID back to the manifest digest r.repoDigest actually
// compares against s.Desired (see the module doc comment). A container
// that does not exist yet (first boot, or before the first Start) is "not
// running", not an error -- there is simply nothing to report on yet.
func (r cliRuntime) Status() (running bool, digest string, restarts int, err error) {
	out, runErr := execCommand(r.bin, "inspect", "-f", "{{.State.Running}} {{.RestartCount}} {{.Image}}", agentContainerName).Output()
	fields := strings.Fields(string(out))
	if runErr != nil || len(fields) < 3 || fields[0] != "true" {
		return false, "", 0, nil
	}
	restarts, _ = strconv.Atoi(fields[1])
	return true, r.repoDigest(fields[2]), restarts, nil
}

// repoDigest resolves imageID's manifest digest for r.repo out of its
// RepoDigests -- the local image ID and the registry manifest digest in
// the state file are two different hashes for the same image, and
// RepoDigests is the only link between them, recorded only when the agent
// pulled by digest (section 13). No matching entry (an unknown source, or
// an image pulled by tag) reports "", which Reconcile then treats as
// "something other than desired is running", never as a false match.
func (r cliRuntime) repoDigest(imageID string) string {
	out, err := execCommand(r.bin, "image", "inspect", "-f", "{{range .RepoDigests}}{{.}} {{end}}", imageID).Output()
	if err != nil {
		return ""
	}
	for _, entry := range strings.Fields(string(out)) {
		if before, digest, ok := strings.Cut(entry, r.repo+"@"); ok && before == "" {
			return digest
		}
	}
	return ""
}
