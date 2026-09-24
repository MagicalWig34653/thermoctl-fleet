// Shared line format for the state file and the health report (section 17,
// 18.3, 22.3): "key=value" per line, "#" starts a comment.
package main

import (
	"bufio"
	"fmt"
	"io"
	"strconv"
	"strings"
)

// readKeyValueLines reads r line by line as "key=value" into a map. Empty
// lines and comments are skipped; a line without "=" is an error. file is
// only used for the error message.
func readKeyValueLines(r io.Reader, file string) (map[string]string, error) {
	values := map[string]string{}
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, found := strings.Cut(line, "=")
		if !found {
			return nil, fmt.Errorf("%s: line without '=': %q", file, line)
		}
		values[strings.TrimSpace(key)] = strings.TrimSpace(value)
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return values, nil
}

// parseOptionalTimestamp reads a Unix-seconds field, empty -> 0.
func parseOptionalTimestamp(value, fieldName, file string) (int64, error) {
	if value == "" {
		return 0, nil
	}
	timestamp, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%s: %q is not a Unix timestamp: %w", file, fieldName, err)
	}
	return timestamp, nil
}
