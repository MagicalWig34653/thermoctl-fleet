package main

import (
	"encoding/json"
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

// parseManifest reads and decodes path -- returns (nil, false, nil) if
// path does not exist at all (nothing pending, not an error), (nil, true,
// err) if it exists but cannot be parsed, or (manifest, true, nil) on
// success. json.Unmarshal alone is used (no third-party dependency,
// stdlib only, section 18.3) -- structural validation of the *values* it
// decodes to (safe paths, non-empty backup_id) is a separate step
// (validateManifestShape below), not this function's job.
func parseManifest(path string) (*Manifest, bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, false, nil
		}
		return nil, true, err
	}
	var manifest Manifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		return nil, true, err
	}
	return &manifest, true, nil
}

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
