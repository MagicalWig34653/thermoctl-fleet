package main

import (
	"encoding/json"
)

// Status is the fixed, small JSON document this program writes for the
// agent to read back and forward to the fleet (P5.5b's `POST
// /v1/restore/result`, `protocol.restore.RestoreResult`) -- this program
// has no fleet connection of its own (no network, per its own top
// docstring), so it can never call that endpoint itself.
//
// **Closed set of fields, closed set of Detail values (details.go)** --
// the same "never anything decrypted, never attacker-influenced free
// text" bound agent/restore.py's own RestoreResult.detail already
// documents applies here identically: this is a report *about* a
// restore, not the restore's content.
type Status struct {
	BackupID  string `json:"backup_id"`
	Result    string `json:"result"`
	Detail    string `json:"detail"`
	Timestamp int64  `json:"timestamp"`
}

const (
	ResultSuccess = "success"
	ResultFailure = "failure"
)

// writeStatusFile writes status atomically -- a fresh temp file in the
// same directory, then os.Rename into place, the same "reader never
// observes a half-written file" guarantee agent.safe_io.write_bytes_safe
// and agent.loop's own report_watchdog_state/report_health/
// report_led_status already give their own status files, ported here for
// this program's own file. The directory itself (path's parent) is
// expected to already exist (created by image preparation /
// systemd's RuntimeDirectory=, see image/common/thermoctl-restore-mover
// .service) -- this function does not create it, the same "nothing
// hard-coded, no directory creation beyond what is explicitly asked for"
// restraint the rest of this program applies throughout.
func writeStatusFile(path string, status Status) error {
	data, err := json.MarshalIndent(status, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')

	// Status files are read by the agent container, not only by this
	// program's own root user -- world-readable, like
	// image/common/tmpfiles.d/thermoctl-agent.conf's own 0755 reasoning
	// for /run/thermoctl-agent applied to a file instead of a directory.
	return writeFileAtomic(path, data, 0o644)
}
