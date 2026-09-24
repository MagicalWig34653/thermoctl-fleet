// The status display on the device: two LEDs on the 40-pin header, Raspberry
// Pi only (section 23), driven by the watchdog, not the agent -- it keeps
// running even when containers are down or the network is gone. Without a
// single dependency (section 23.1): no GPIO package, no ioctl. The image
// recipe loads the `gpio-led` kernel overlays; after that the watchdog only
// writes to two sysfs files.
package main

import (
	"fmt"
	"os"
)

// Paths of the two LED brightness files, created by the `gpio-led` overlay
// (section 23.1) -- fixed, because they are bound to the GPIO numbers in
// `config.txt`, not to a configuration of this program.
const (
	LedDevicePath = "/sys/class/leds/thermoctl-device/brightness"
	LedSystemPath = "/sys/class/leds/thermoctl-system/brightness"
)

// LedPresent checks whether one of the two files exists. If it is missing
// (no Raspberry Pi, overlay not loaded), that is explicitly **not** an
// error -- the display is optional, the watchdog keeps working without it
// unchanged (section 23.3).
func LedPresent(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// LedSetPattern writes a blink pattern for the LED under path (section
// 23.2: off/on/slow blink/fast blink/two short blinks), via the kernel
// `timer` trigger (`delay_on`/`delay_off`), not yet implemented -- which
// watchdog state (AgentStopped, AwaitHealthReport, ...) triggers which
// pattern is decided together with watch.go.
//
// If the file is missing, nothing is attempted and no error is reported
// (see LedPresent) -- a device without this header must not be blocked by
// this.
func LedSetPattern(path string, pattern string) error {
	if !LedPresent(path) {
		return nil
	}
	return fmt.Errorf("driving LED %q (pattern %q) not implemented -- see docs/specification.md section 23", path, pattern)
}
