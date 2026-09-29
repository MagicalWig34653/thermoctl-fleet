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
//     before open (never follow a symlink), regular files only, a
//     closed allowlist of relative names, no ".." or absolute paths, and
//     size/sha256 checked against the manifest, computed from the
//     already-open file descriptor (not a second stat of the path,
//     which would reopen the same TOCTOU window agent/restore.py's own
//     module docstring describes as "not this process's to fully close
//     on its own" -- this program is that close).
//  3. The staging directory's own contents are compared against the
//     manifest in both directions: every manifest entry must exist and
//     validate, and every entry actually present in staging must be
//     accounted for by the manifest (no silent extra file).
//
// Order: validate everything first, then move. A move failure partway
// through is reported honestly as a partial failure; whatever is left in
// staging stays there untouched (never deleted on failure) so a later run
// -- after the underlying problem, e.g. a full disk, is fixed -- can
// finish the job without asking the landlord to restore a second time.
// Staging (including the manifest) is removed only once every file has
// moved successfully.
package main
