// thermoctl-restore-mover -- P5.5c, the other half of section 15.3's
// second "Decided afterward" paragraph (2026-09-28): "Moving it into the
// real data directories is done by a small, separate Go program on the
// bare system next to the watchdog (same module, no dependency, no
// network, section 18.4's language rule), and only if no operational data
// exists there yet." agent/restore.py (P5.5b) only ever stages a
// decrypted restore into its own directory plus a manifest
// (agent.restore.MANIFEST_FILENAME); this program is the only thing that
// ever moves that staged data into thermoctl_db_path/zigbee2mqtt_dir.
//
// Constraints (CLAUDE.md security principle 6, docs/specification.md
// section 18.3/18.4, mirrored exactly for this program): no third-party
// dependency (same go.mod as the watchdog and cmd/thermoctl-leds), no
// network, no registry -- only the local filesystem and (for the status
// file) a local file the agent polls. Not counted toward the watchdog's
// own 300-statement-line budget (a separate program, like
// cmd/thermoctl-leds already is), but kept small and plain regardless.
//
// **CLAUDE.md security principle 5: the agent is the security boundary,
// not the cloud -- but neither is the agent the boundary here.** This
// program never trusts anything the agent (a process a compromised or
// buggy cloud could ultimately influence) already checked -- it re-checks,
// authoritatively, from a process the agent has no way to influence
// beyond the bytes it already wrote to the staging directory:
//
//  1. The live directories are re-checked empty here (liveStoreIsEmpty),
//     using exactly the same definition agent/restore.py's own advisory
//     check (_operational_store_is_empty) uses -- this is a shared
//     contract between the two, not a private detail of either: a
//     PLAIN thermoctl.db that exists but is 0 bytes, or a
//     Zigbee2MQTT directory with neither database.db nor
//     coordinator_backup.json, both still count as "empty" on both
//     sides. A change to that definition on either side without the
//     other is a contract break, not a private refactor.
//  2. Every file named in the manifest is validated from scratch: lstat
//     before open (never follow a symlink), regular files only, **exactly
//     one hard link** (cross-review finding -- a multiply-linked file
//     would let a second, unrelated path to the same inode be mutated by
//     this program's own chown/chmod, or by whatever wrote through the
//     other link while this program was reading it), a closed allowlist
//     of relative names, no ".." or absolute paths, and size/sha256
//     checked against the manifest.
//  3. The staging directory's own contents are compared against the
//     manifest in both directions: every manifest entry must exist and
//     validate, and every entry actually present in staging must be
//     accounted for by the manifest (no silent extra file).
//  4. manifest.json itself is opened with the same lstat/O_NOFOLLOW
//     discipline and capped at MaxManifestBytes before being parsed --
//     it is written by the same untrusted agent process as everything
//     else under staging.
//
// **Copy, never move the staged file directly (cross-review finding,
// design change from an earlier os.Rename-based version of this
// program):** a staged file is opened once, safely, and its bytes are
// read, hashed, *and* copied -- in one continuous pass from that single
// open descriptor -- into a brand-new file this program itself creates
// inside the real destination directory (O_CREAT|O_EXCL|O_NOFOLLOW,
// random name, mode 0600), which is then fsynced, fchown/fchmod'd (via
// the descriptor, never by path), and only *then* renamed into its final
// name -- always a same-directory rename, so it can never fail with
// EXDEV. **This closes the TOCTOU window a "validate, then later
// os.Rename the staged path" design leaves open**: the staging directory
// stays writable by the untrusted agent process for as long as this
// program runs, so a validate/use split lets that process swap the
// staged path for a symlink (or a hard link elsewhere) in between --
// reading, hashing, and writing from one held-open descriptor is
// immune to any of that regardless of what happens to the *path*
// afterward, because the descriptor never referred to the path, only to
// the inode it resolved to at open time. See validate.go's own
// preparedFile docstring for the exact sequence.
//
// Order: validate and copy every file first (into temporary files under
// their real destination directories), and only rename any of them into
// their final names once every single one has copied and verified
// cleanly. A rename failure partway through is reported honestly as a
// partial failure; whatever was already renamed stays renamed, any
// still-pending temporary files are removed, and whatever is left in
// `stagingDir` stays there untouched (never deleted on failure) so a
// later run -- after the underlying problem, e.g. a full disk, is fixed
// -- can finish the job without asking the landlord to restore a second
// time. **P5.5d: a partial finalize is resumable, not stuck forever** --
// before the first rename of a run, journal.go's own pre-rename journal
// records exactly what is about to be renamed into place; a later run
// that finds the live store non-empty consults it (canResumeFinalize)
// and, only if every live file that exists still matches it exactly,
// re-validates staging from scratch and finishes the remaining renames
// instead of refusing forever. Staging's *contents* (the manifest
// included, never the staging directory entry itself -- P5.5d, see
// move.go::removeStagingContents) and the journal are removed only once
// every file has been renamed into place successfully.
package main
