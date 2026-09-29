package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// run is main's own logic, factored out for a direct test (the same
// shape watchdog/main.go and cmd/thermoctl-leds/loop.go already use) --
// takes an explicit now func so tests never depend on a real clock.
//
// Exit codes: 0 for "nothing to do" or "applied successfully", 1 for
// every refusal/failure (still writes a status file wherever it has
// enough information to -- see each branch below for when it does not).
func run(cfg Config, warn func(format string, args ...any), now func() time.Time) int {
	manifestPath := filepath.Join(cfg.StagingDir, ManifestFilename)
	manifest, manifestBytes, present, err := parseManifest(manifestPath)
	if !present {
		if err != nil {
			warn("thermoctl-restore-mover: reading %s: %v", manifestPath, err)
			return 1
		}
		// No manifest at all -- nothing pending. Not an error, and
		// nothing is written to the status file: a status file describes
		// the outcome of an actual restore attempt, and there was none.
		return 0
	}
	if err != nil {
		detail := DetailManifestMalformed
		if err == errManifestTooLarge {
			detail = DetailManifestTooLarge
		}
		writeOutcome(cfg, warn, now, "", ResultFailure, detail)
		return 1
	}
	manifestSHA256 := sha256Hex(manifestBytes)

	targets := Targets{ThermoctlDBPath: cfg.ThermoctlDBPath, Zigbee2mqttDir: cfg.Zigbee2mqttDir}
	empty, err := liveStoreIsEmpty(targets)
	if err != nil {
		warn("thermoctl-restore-mover: checking live store emptiness: %v", err)
		writeOutcome(cfg, warn, now, manifest.BackupID, ResultFailure, DetailLiveStoreNotEmpty)
		return 1
	}
	if !empty {
		// P5.5d: a non-empty live store is not necessarily "someone
		// else's data" -- it may be exactly what *this program itself*
		// already wrote during an earlier run that failed partway
		// through finalizeAll. canResumeFinalize decides, authoritatively
		// (never trusting the journal's mere presence), whether that is
		// provably the case for this exact manifest -- see journal.go's
		// own top docstring for the full reasoning. Anything else still
		// refuses exactly as before.
		if !canResumeFinalize(cfg.JournalFilePath, manifest, manifestSHA256, targets) {
			writeOutcome(cfg, warn, now, manifest.BackupID, ResultFailure, DetailLiveStoreNotEmpty)
			return 1
		}
		warn("thermoctl-restore-mover: live store matches this program's own journal for backup %s; resuming the interrupted finalize", manifest.BackupID)
	}

	prepared, err := validateAll(cfg.StagingDir, manifest, targets)
	if err != nil {
		warn("thermoctl-restore-mover: validation failed: %v", err)
		writeOutcome(cfg, warn, now, manifest.BackupID, ResultFailure, detailOf(err, DetailUnsafeStaging))
		return 1
	}

	// The journal is written *before* the first rename -- see journal.go's
	// own top docstring for why: a crash between this write and the last
	// rename must always leave behind a journal describing exactly what
	// was about to happen, so a later run can tell "I already did this"
	// apart from "something unrelated is live here".
	journal := buildJournal(manifest.BackupID, manifestSHA256, prepared)
	if err := writeJournal(cfg.JournalFilePath, journal); err != nil {
		warn("thermoctl-restore-mover: writing journal %s: %v", cfg.JournalFilePath, err)
		cleanupPrepared(prepared)
		writeOutcome(cfg, warn, now, manifest.BackupID, ResultFailure, DetailJournalWriteFailed)
		return 1
	}

	result := finalizeAll(prepared, warn)
	if !result.complete() {
		warn("thermoctl-restore-mover: renamed %d/%d files before failing; leaving the rest staged", result.Renamed, result.Total)
		writeOutcome(cfg, warn, now, manifest.BackupID, ResultFailure, DetailPartialMove)
		return 1
	}

	if err := removeStagingContents(cfg.StagingDir, manifest); err != nil {
		// Every file is already safely in its destination at this point
		// -- a failure to clean up staging afterward must not be reported
		// as a failed restore (that would be exactly the kind of
		// misleading report agent/restore.py's own module docstring
		// warns against for its "staged, awaiting apply" wording,
		// applied here to "applied" itself). Logged, not fatal.
		warn("thermoctl-restore-mover: %v (data already moved; staging left behind for manual cleanup)", err)
	}
	if err := removeJournal(cfg.JournalFilePath); err != nil {
		// Same reasoning: the restore already fully succeeded, a leftover
		// journal is harmless (it names a manifest that no longer exists
		// in staging, so it can never be mistaken for a still-pending
		// resumption) and must not turn a success into a reported
		// failure.
		warn("thermoctl-restore-mover: removing journal %s: %v (data already moved; harmless leftover)", cfg.JournalFilePath, err)
	}

	writeOutcome(cfg, warn, now, manifest.BackupID, ResultSuccess, DetailApplied)
	return 0
}

func writeOutcome(cfg Config, warn func(format string, args ...any), now func() time.Time, backupID, result, detail string) {
	status := Status{BackupID: backupID, Result: result, Detail: detail, Timestamp: now().Unix()}
	if err := writeStatusFile(cfg.StatusFilePath, status); err != nil {
		warn("thermoctl-restore-mover: writing status file %s: %v", cfg.StatusFilePath, err)
	}
}

// Config bundles every path this program needs -- nothing hard-coded
// (CLAUDE.md's own working-method rule applied here too), every value a
// flag with a documented, on-device default below.
type Config struct {
	StagingDir      string
	ThermoctlDBPath string
	Zigbee2mqttDir  string
	StatusFilePath  string
	// JournalFilePath (P5.5d): this program's own pre-rename journal --
	// see journal.go's own top docstring. Lives in the same persistent,
	// root-owned state directory as StatusFilePath
	// (/var/lib/thermoctl-restore-mover), never in the agent-writable
	// staging directory.
	JournalFilePath string
}

func main() {
	stagingDir := flag.String("staging-dir", "/var/lib/thermoctl-agent/pending-restore", "agent/restore.py's own RestoreTargets.staging_dir (P5.5b) -- where a staged restore and its manifest.json wait")
	thermoctlDBFile := flag.String("thermoctl-db-file", "/var/lib/thermoctl/thermoctl.db", "thermoctl's live SQLite database file (section 15.2) -- matches agent/__main__.py's own --thermoctl-db-file default")
	zigbee2mqttDir := flag.String("zigbee2mqtt-dir", "/var/lib/zigbee2mqtt", "Zigbee2MQTT's live data directory (section 15.2) -- matches agent/__main__.py's own --zigbee2mqtt-dir default")
	statusFile := flag.String("status-file", "/var/lib/thermoctl-restore-mover/status.json", "where this program reports the outcome for the agent to read back and forward to the fleet (P5.5b's POST /v1/restore/result) -- persistent, not /run (P5.5d): no reason this report needs to be wiped on reboot the way the agent's own health report does")
	journalFile := flag.String("journal-file", "/var/lib/thermoctl-restore-mover/journal.json", "P5.5d: this program's own pre-rename journal (journal.go) -- root-owned, persistent, never under the agent-writable staging directory")
	flag.Parse()

	code := run(Config{
		StagingDir:      *stagingDir,
		ThermoctlDBPath: *thermoctlDBFile,
		Zigbee2mqttDir:  *zigbee2mqttDir,
		StatusFilePath:  *statusFile,
		JournalFilePath: *journalFile,
	}, func(format string, args ...any) {
		fmt.Fprintf(os.Stderr, format+"\n", args...)
	}, time.Now)
	os.Exit(code)
}
