package main

// Bounded, closed set of status-file detail values this program ever
// writes -- listed together, the same "easy to audit" reasoning
// agent/restore.py's own DETAIL_* constants give for their closed set.
// **Never anything decrypted, never a path outside this fixed list** --
// nothing here can ever carry a byte of tenant data.
const (
	DetailApplied            = "applied"
	DetailLiveStoreNotEmpty  = "live operational data store is not empty"
	DetailNoRestorePending   = "no staged restore is pending"
	DetailManifestMalformed  = "staged manifest is malformed"
	DetailManifestTooLarge   = "staged manifest exceeds the maximum allowed size"
	DetailUnsafeStaging      = "staging directory is unsafe (symlink or not a directory)"
	DetailUnsafeDestination  = "destination directory is unsafe (symlink or not a directory)"
	DetailUnexpectedEntry    = "staging directory contains an entry not listed in the manifest"
	DetailUnknownFileName    = "manifest names a file outside the known set"
	DetailUnsafePath         = "manifest path is unsafe (absolute, escapes staging, or not clean)"
	DetailFileMissing        = "a file named in the manifest is missing from staging"
	DetailNotRegularFile     = "a file named in the manifest is not a regular file"
	DetailHardLinkedFile     = "a file named in the manifest has more than one hard link"
	DetailFileTooLarge       = "a staged file exceeds the maximum allowed size"
	DetailSizeMismatch       = "staged file size does not match the manifest"
	DetailHashMismatch       = "staged file sha256 does not match the manifest"
	DetailDestinationMissing = "destination directory does not exist"
	DetailPartialMove        = "move failed partway; remaining files left staged"
)
