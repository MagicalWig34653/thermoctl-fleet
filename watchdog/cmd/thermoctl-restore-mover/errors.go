package main

// detailedError wraps one of the closed DETAIL_* strings (details.go) --
// every refusal in this program is expressed as one of these, so the
// caller (main.go) always has a status-file-ready detail string to write,
// never a raw, unbounded Go error message that could leak a path or
// wrapped I/O detail into the status file the agent reads and forwards to
// the fleet (CLAUDE.md security principle 4 applied to a report about a
// restore, the same reasoning agent/restore.py's own DETAIL_* comment
// gives).
type detailedError struct {
	detail string
}

func (e *detailedError) Error() string {
	return e.detail
}

func errUnsafe(detail string) error {
	return &detailedError{detail: detail}
}

// detailOf extracts the closed detail string from err if it is a
// detailedError, or returns fallback otherwise -- used by main.go so an
// unexpected, non-detailed error (a genuine bug, or an I/O error this
// program did not anticipate) still produces a status file with a value
// from the closed set, never a raw error string.
func detailOf(err error, fallback string) string {
	if de, ok := err.(*detailedError); ok {
		return de.detail
	}
	return fallback
}
