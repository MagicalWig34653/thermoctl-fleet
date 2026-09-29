package main

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	pathpkg "path"
)

// Manifest mirrors exactly what agent/restore.py::_write_manifest writes
// to {staging_dir}/manifest.json -- the shared contract between the two
// programs (docs/STATUS.md's P5.5c section). A field added on one side
// without the other is a contract break.
type Manifest struct {
	BackupID string         `json:"backup_id"`
	StagedAt string         `json:"staged_at"`
	Files    []ManifestFile `json:"files"`
}

// ManifestFile is one entry of Manifest.Files -- Path is relative to the
// staging directory, exactly as agent/restore.py::_StagedFile.relative_path
// writes it (forward-slash separated, e.g. "zigbee2mqtt/database.db").
type ManifestFile struct {
	Path      string `json:"path"`
	SizeBytes int64  `json:"size_bytes"`
	SHA256    string `json:"sha256"`
}

// ManifestFilename is the fixed name agent/restore.py::MANIFEST_FILENAME
// also uses -- its presence is what both this program's caller (the
// systemd path unit, see image/common/thermoctl-restore-mover.path) and
// the agent's own _staged_restore_already_pending use as "is a staged
// restore waiting".
const ManifestFilename = "manifest.json"

// allowedManifestPaths is the closed allowlist of relative paths this
// program will ever move -- exactly the three members
// agent/restore.py::apply_pending_restore can ever stage (thermoctl.db,
// and the two Zigbee2MQTT files, both only ever staged together under a
// fixed "zigbee2mqtt/" subdirectory). A manifest naming anything else is
// refused outright, never merely ignored -- CLAUDE.md security principle
// 5: this program is the security boundary here, and a name outside this
// set is exactly the kind of thing a compromised or buggy agent process
// must not be able to walk this program into moving.
var allowedManifestPaths = map[string]struct{}{
	"thermoctl.db":                        {},
	"zigbee2mqtt/database.db":             {},
	"zigbee2mqtt/coordinator_backup.json": {},
}

// MaxManifestBytes bounds how much of manifest.json this program will
// ever read (cross-review finding, P5.5c) -- the manifest is itself
// agent-controlled (staged by the same process this program never
// trusts, CLAUDE.md security principle 5), and without a cap a
// maliciously or accidentally huge manifest.json would be read fully into
// memory before any other check ever runs. 64 KiB is generous headroom
// over the largest manifest this contract ever actually produces (three
// fixed file entries, a handful of bytes each) -- see
// agent/restore.py::_write_manifest's own docstring for the fixed,
// closed shape it writes.
const MaxManifestBytes = 64 * 1024

// parseManifest reads and decodes path -- returns (nil, nil, false, nil) if
// path does not exist at all (nothing pending, not an error), (nil, nil,
// true, err) if it exists but cannot be parsed or opened safely, (nil, nil,
// true, errManifestTooLarge) if it exceeds MaxManifestBytes, or (manifest,
// rawBytes, true, nil) on success. Opened via openRegularNoFollow (lstat
// before open, O_NOFOLLOW, regular files only -- the manifest gets exactly
// the same discipline every staged data file already gets, since it is
// written by the same untrusted process) rather than a plain os.ReadFile.
// json.Unmarshal alone is used to decode (no third-party dependency,
// stdlib only, section 18.3) -- structural validation of the *values* it
// decodes to (safe paths, non-empty backup_id) is a separate step
// (validateManifestShape below), not this function's job.
//
// **The raw bytes are also returned (P5.5d)**: journal.go's own
// pre-rename journal records a sha256 of the whole manifest file, keyed
// against exactly these same bytes -- re-reading the file a second time
// to hash it would reopen a TOCTOU window this program otherwise avoids
// everywhere else (the agent could, in principle, rewrite manifest.json
// between two separate reads); returning the bytes already read here
// keeps "read once, use everywhere" for this file too.
func parseManifest(path string) (*Manifest, []byte, bool, error) {
	file, _, err := openRegularNoFollow(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil, false, nil
		}
		return nil, nil, true, err
	}
	defer file.Close()

	data, err := io.ReadAll(io.LimitReader(file, MaxManifestBytes+1))
	if err != nil {
		return nil, nil, true, err
	}
	if len(data) > MaxManifestBytes {
		return nil, nil, true, errManifestTooLarge
	}

	var manifest Manifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		return nil, nil, true, err
	}
	return &manifest, data, true, nil
}

var errManifestTooLarge = errors.New("manifest exceeds the maximum allowed size")

// validateManifestShape checks the manifest's own structural well-
// formedness -- independent of what is actually present in staging
// (validateStagingContents below checks that): a non-empty backup_id, at
// least one file, no duplicate relative paths, and every relative path
// safe and in the closed allowlist.
func validateManifestShape(manifest *Manifest) error {
	if manifest.BackupID == "" {
		return errUnsafe(DetailManifestMalformed)
	}
	if len(manifest.Files) == 0 {
		return errUnsafe(DetailManifestMalformed)
	}
	seen := map[string]struct{}{}
	for _, file := range manifest.Files {
		if _, duplicate := seen[file.Path]; duplicate {
			return errUnsafe(DetailManifestMalformed)
		}
		seen[file.Path] = struct{}{}

		if !isSafeManifestPath(file.Path) {
			return errUnsafe(DetailUnsafePath)
		}
		if _, known := allowedManifestPaths[file.Path]; !known {
			return errUnsafe(DetailUnknownFileName)
		}
		if file.SizeBytes < 0 || file.SHA256 == "" {
			return errUnsafe(DetailManifestMalformed)
		}
	}
	return nil
}

// isSafeManifestPath refuses an absolute path, a path that is not already
// in its own cleaned form (e.g. a redundant "./", a trailing slash, or
// "a//b"), and any path containing a ".." segment anywhere -- the same
// "relative, clean, no .., no absolute" condition the task spec calls for,
// checked with the "path" package (forward-slash semantics) rather than
// "path/filepath": the manifest's own paths are always "/"-separated, by
// construction on the Python side, regardless of what OS this program
// happens to run on.
func isSafeManifestPath(p string) bool {
	if p == "" || pathpkg.IsAbs(p) {
		return false
	}
	if pathpkg.Clean(p) != p {
		return false
	}
	if p == "." || p == ".." {
		return false
	}
	for _, segment := range splitPath(p) {
		if segment == ".." || segment == "" {
			return false
		}
	}
	return true
}

func splitPath(p string) []string {
	var segments []string
	start := 0
	for i := 0; i < len(p); i++ {
		if p[i] == '/' {
			segments = append(segments, p[start:i])
			start = i + 1
		}
	}
	segments = append(segments, p[start:])
	return segments
}
