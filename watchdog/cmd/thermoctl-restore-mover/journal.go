package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
)

// P5.5d -- resumable finalize.
//
// **The problem this file fixes:** `finalizeAll` (move.go) can fail partway
// through -- one manifest entry already renamed into its live destination,
// a later one still not. Before this package, main.go's own `run` always
// re-checked `liveStoreIsEmpty` first and refused outright
// (`DetailLiveStoreNotEmpty`) the moment it was not -- which, after a
// partial finalize, is *forever*: the live store is no longer empty
// precisely because this program itself already (partially) succeeded, and
// nothing ever clears that state automatically. A landlord stuck here had
// no way forward short of a manual, undocumented intervention.
//
// **The fix: a small journal, written once, before the first rename.**
// `writeJournal` persists -- to this program's own root-owned, persistent
// state directory (`/var/lib/thermoctl-restore-mover`, *never* the
// agent-writable staging directory: the agent is explicitly untrusted,
// CLAUDE.md security principle 5, and must never be able to plant or edit
// this program's own record of what it already did) -- exactly the
// `backup_id`, a sha256 of the *whole* manifest file (not only the
// `backup_id`, which the agent could in principle reuse across two
// different stagings), and the final destination path plus expected
// sha256 of every file this run is about to rename into place. This is
// written *after* `validateAll` has already fully validated and copied
// every file into its temporary destination-side location, but *before*
// `finalizeAll` renames a single one of them -- so a run that crashes,
// loses power, or otherwise dies between the journal write and the last
// rename always leaves a journal on disk that describes exactly what was
// about to happen.
//
// **On a later run, `canResumeFinalize` decides whether a non-empty live
// store may still be proceeded with, never trusting the journal's mere
// presence alone** (CLAUDE.md security principle 5, applied to this
// program's own state as much as to anything the agent wrote): it
// requires a journal that names *this exact* manifest (same `backup_id`
// *and* the same manifest sha256 -- a reused or coincidentally-identical
// `backup_id` for a genuinely different staging is refused, not silently
// accepted), and it re-reads every live operational file the journal
// remembers writing and re-hashes it from disk right now -- a live file
// that no longer matches its journaled hash (tampered, corrupted, or
// touched by something other than this program since the partial run)
// refuses resumption entirely, as does a live operational file that
// exists but is not accounted for in the journal at all. Only if every
// live file still matches, exactly, does this program go on to
// re-validate staging from scratch (the same `validateAll` every fresh
// run already goes through) and finish the remaining renames -- a file
// already correctly in place simply gets renamed onto again with the
// identical bytes, a harmless no-op rename.
//
// **The journal is cleared only once a run completes every rename
// successfully** (`removeJournal`, called from the same place
// `removeStagingContents` already is) -- mirroring every other "only ever
// removed after full, verified success" discipline in this program.
type Journal struct {
	BackupID       string        `json:"backup_id"`
	ManifestSHA256 string        `json:"manifest_sha256"`
	Files          []JournalFile `json:"files"`
}

// JournalFile is one file this run is about to rename into place --
// FinalPath is the live, absolute destination path (not the staging-
// relative manifest path: what matters for resumption is what is
// actually on the live filesystem).
type JournalFile struct {
	FinalPath string `json:"final_path"`
	SHA256    string `json:"sha256"`
}

// JournalFilename is the fixed name of the journal file inside this
// program's own state directory.
const JournalFilename = "journal.json"

// MaxJournalBytes bounds how much of journal.json this program will ever
// read back -- the same "cap before parsing" discipline MaxManifestBytes
// already applies to the (agent-written) manifest, applied here to a file
// only this program itself ever writes, purely as defense in depth (a
// corrupted or truncated journal must fail closed, not hang reading an
// unbounded file).
const MaxJournalBytes = 64 * 1024

func buildJournal(backupID, manifestSHA256 string, prepared []preparedFile) Journal {
	files := make([]JournalFile, 0, len(prepared))
	for _, one := range prepared {
		files = append(files, JournalFile{FinalPath: one.FinalPath, SHA256: one.SHA256})
	}
	return Journal{BackupID: backupID, ManifestSHA256: manifestSHA256, Files: files}
}

// writeJournal persists journal atomically (writeFileAtomic,
// atomicwrite.go) -- mode 0600, root-only: unlike the status file, the
// agent has no legitimate reason to ever read this program's own journal.
func writeJournal(path string, journal Journal) error {
	data, err := json.MarshalIndent(journal, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return writeFileAtomic(path, data, 0o600)
}

// removeJournal deletes the journal file -- called only once a run's
// finalizeResult.complete() is true, alongside removeStagingContents.
// Missing is not an error (nothing to clean up, e.g. a run that never got
// as far as writing one).
func removeJournal(path string) error {
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// readJournal reads back a previously written journal via the same
// lstat/O_NOFOLLOW discipline every other file this program reads gets
// (openRegularNoFollow) -- a missing, symlinked, non-regular, oversized,
// or structurally invalid journal is all treated identically as "no
// usable journal" (present=false), never as a hard error: the caller's
// only use for this is "may I resume?", and every one of these cases
// means "no, not verifiably" -- fail closed, exactly like every other
// check in this program.
func readJournal(path string) (*Journal, bool) {
	file, _, err := openRegularNoFollow(path)
	if err != nil {
		return nil, false
	}
	defer file.Close()

	data, err := io.ReadAll(io.LimitReader(file, MaxJournalBytes+1))
	if err != nil || len(data) > MaxJournalBytes {
		return nil, false
	}
	var journal Journal
	if err := json.Unmarshal(data, &journal); err != nil {
		return nil, false
	}
	return &journal, true
}

// canResumeFinalize is the authoritative decision of whether a non-empty
// live store may still be proceeded with -- see this file's own top
// docstring for the full reasoning. Every failure mode (no journal, a
// journal for a different manifest, a live file that does not match, a
// live file the journal never accounted for, a live file that can no
// longer be safely read) returns false -- there is no partial-credit
// path here.
func canResumeFinalize(journalPath string, manifest *Manifest, manifestSHA256 string, targets Targets) bool {
	journal, present := readJournal(journalPath)
	if !present {
		return false
	}
	if journal.BackupID != manifest.BackupID || journal.ManifestSHA256 != manifestSHA256 {
		return false
	}

	journaled := make(map[string]string, len(journal.Files))
	for _, f := range journal.Files {
		journaled[f.FinalPath] = f.SHA256
	}

	for _, live := range knownLiveOperationalPaths(targets) {
		exists, err := pathExists(live)
		if err != nil {
			return false
		}
		if !exists {
			continue
		}
		expected, known := journaled[live]
		if !known {
			// A live operational file exists that this program's own
			// journal never wrote -- something this run cannot account
			// for, refuse rather than guess.
			return false
		}
		actual, err := sha256OfExistingFile(live)
		if err != nil || actual != expected {
			return false
		}
	}
	return true
}

// knownLiveOperationalPaths lists every live destination path this
// program could ever have written to -- the same closed set
// destinationFor (validate.go) maps the manifest's own allowlisted
// relative paths onto, restated here so canResumeFinalize can check "is
// there anything live that the journal did not account for" without
// needing a manifest that might not name every one of them (a resumed
// run's manifest could, in principle, list fewer than three files if a
// backup never had Zigbee2MQTT data at all -- the live-side check must
// still cover every path this program is ever capable of writing to,
// not only the ones the current manifest happens to mention).
func knownLiveOperationalPaths(targets Targets) []string {
	return []string{
		targets.ThermoctlDBPath,
		targets.Zigbee2mqttDir + "/database.db",
		targets.Zigbee2mqttDir + "/coordinator_backup.json",
	}
}

// sha256OfExistingFile hashes a live file via the same lstat/O_NOFOLLOW
// discipline as every staged file (openRegularNoFollow) -- a symlink
// planted at a live path since the partial run is refused here exactly
// like everywhere else in this program, never followed.
func sha256OfExistingFile(path string) (string, error) {
	file, _, err := openRegularNoFollow(path)
	if err != nil {
		return "", err
	}
	defer file.Close()

	hasher := sha256.New()
	if _, err := io.Copy(hasher, io.LimitReader(file, MaxStagedFileBytes+1)); err != nil {
		return "", err
	}
	return hex.EncodeToString(hasher.Sum(nil)), nil
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
