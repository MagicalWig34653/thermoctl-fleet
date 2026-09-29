package main

import (
	"fmt"
	"os"
	"path/filepath"
)

// writeFileAtomic writes data to path via a fresh temp file created in the
// same directory, fsynced, chmod'd, then renamed into place -- "a reader
// never observes a half-written file", the same guarantee
// agent.safe_io.write_bytes_safe and this program's own status.go already
// give their respective files, factored out here (P5.5d) so journal.go's
// own pre-rename journal gets the identical guarantee without duplicating
// the sequence a second time.
func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	dir := filepath.Dir(path)
	temp, err := os.CreateTemp(dir, ".thermoctl-restore-mover-*.tmp")
	if err != nil {
		return fmt.Errorf("creating temporary file in %s: %w", dir, err)
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath) // no-op once the rename below succeeds

	if _, err := temp.Write(data); err != nil {
		temp.Close()
		return fmt.Errorf("writing temporary file: %w", err)
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return fmt.Errorf("fsyncing temporary file: %w", err)
	}
	if err := temp.Close(); err != nil {
		return fmt.Errorf("closing temporary file: %w", err)
	}
	if err := os.Chmod(tempPath, mode); err != nil {
		return fmt.Errorf("chmod temporary file: %w", err)
	}
	if err := os.Rename(tempPath, path); err != nil {
		return fmt.Errorf("renaming file into place: %w", err)
	}
	return nil
}
