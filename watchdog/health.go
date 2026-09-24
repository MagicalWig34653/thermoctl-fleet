// The health report (section 17, step 5). Line-based like the state file,
// not a single timestamp (section 22.3, decided afterward): `timestamp=`,
// `digest=`, `version=`. The gain is `digest`, the **currently running**
// digest of the writing container revision -- this way the watchdog sees
// that the right thing is alive, not just something.
package main

import (
	"fmt"
	"io"
	"os"
	"strconv"
)

// Health is the parsed content of the health report file.
type Health struct {
	Timestamp int64
	Digest    string
	Version   string
}

// ParseHealth reads a health report file from r. Unknown keys are ignored,
// not rejected (as with the state file).
func ParseHealth(r io.Reader) (Health, error) {
	values, err := readKeyValueLines(r, "health report")
	if err != nil {
		return Health{}, err
	}
	if values["timestamp"] == "" {
		return Health{}, fmt.Errorf("health report: 'timestamp' is missing or empty")
	}
	timestamp, err := strconv.ParseInt(values["timestamp"], 10, 64)
	if err != nil {
		return Health{}, fmt.Errorf("health report: 'timestamp' is not a Unix timestamp: %w", err)
	}
	return Health{Timestamp: timestamp, Digest: values["digest"], Version: values["version"]}, nil
}

// ReadHealth opens path and parses it.
func ReadHealth(path string) (Health, error) {
	file, err := os.Open(path)
	if err != nil {
		return Health{}, err
	}
	defer file.Close()
	return ParseHealth(file)
}
