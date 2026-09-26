// Package ledsysfs drives the two status LEDs (section 23) through the
// kernel LED class driver only -- no GPIO library, no ioctl (section 23.1).
// Used exclusively by cmd/thermoctl-leds, the separate small program the
// project owner decided on 2026-09-26 (see docs/specification.md section
// 23, "Decided afterward"): the watchdog itself no longer imports this
// package, which is exactly why it lives under internal/ rather than in
// the watchdog's own root package -- moving it here is what gave the
// watchdog back the line-budget headroom P5.6 left at only one line (see
// docs/STATUS.md's P5.7 entry for the before/after count).
//
// The image recipe loads the `gpio-led` overlays (section 23.1); after
// that this package only writes to plain sysfs files -- brightness,
// trigger, delay_on, delay_off -- reproducible by hand with `echo`.
package ledsysfs

import (
	"fmt"
	"os"
	"path/filepath"
)

// Pattern is one of the blink patterns section 23.2 defines for the two
// LEDs. Not every pattern applies to both LEDs (LED 1 has five, LED 2 only
// three) -- cmd/thermoctl-leds's own decision code enforces that, this
// package only knows how to *render* a pattern once decided.
type Pattern string

const (
	Off            Pattern = "off"
	SteadyOn       Pattern = "steady-on"
	SlowBlink      Pattern = "slow-blink"
	FastBlink      Pattern = "fast-blink"
	TwoShortBlinks Pattern = "two-short-blinks"
)

// LedPresent checks whether the LED's brightness file exists. If it is
// missing (no Raspberry Pi, overlay not loaded), that is explicitly **not**
// an error -- the display is optional, section 23.3.
func LedPresent(brightnessPath string) bool {
	_, err := os.Stat(brightnessPath)
	return err == nil
}

// ApplyPattern writes the sysfs files for pattern under the LED directory
// that brightnessPath lives in. If the LED is not present, nothing is
// attempted and no error is reported (see LedPresent) -- a device without
// this header must not be blocked by this, and must not spam its log
// every poll interval either.
//
// The kernel `timer` trigger (section 23.1) exposes exactly one on/off
// period, not a sequence -- so "two short blinks, pause" (section 23.2,
// LED 1's "no cloud contact") is necessarily an **approximation**: a
// short pulse with a longer pause (100ms on, 700ms off), distinguishable
// by ear and by eye from the continuous, even "fast blink" used for
// "waiting for assignment" (100ms on, 100ms off), but not a true grouped
// double-flash. A true double-flash would need either a software blink
// loop in this program (defeating section 23.1's own point: the display
// keeps blinking even if this program is delayed or briefly not
// scheduled) or the kernel's separate `pattern` trigger, whose
// availability is not guaranteed across kernel configurations the way the
// always-present `timer` trigger's is. Documented trade-off, not an
// oversight -- see docs/STATUS.md's P5.7 entry.
func ApplyPattern(brightnessPath string, pattern Pattern) error {
	if !LedPresent(brightnessPath) {
		return nil
	}
	dir := filepath.Dir(brightnessPath)
	trigger := filepath.Join(dir, "trigger")
	delayOn := filepath.Join(dir, "delay_on")
	delayOff := filepath.Join(dir, "delay_off")

	switch pattern {
	case Off:
		return writeSteady(trigger, brightnessPath, "0")
	case SteadyOn:
		return writeSteady(trigger, brightnessPath, "1")
	case SlowBlink:
		return writeTimer(trigger, delayOn, delayOff, "500", "500")
	case FastBlink:
		return writeTimer(trigger, delayOn, delayOff, "100", "100")
	case TwoShortBlinks:
		return writeTimer(trigger, delayOn, delayOff, "100", "700")
	default:
		return fmt.Errorf("ledsysfs: unknown pattern %q", pattern)
	}
}

// writeSteady sets the LED to a fixed brightness with no trigger --
// "trigger" is set first, matching the real kernel's own attribute
// dependency (switching away from "timer" is what makes delay_on/
// delay_off disappear again; brightness is only meaningful once no
// trigger is driving it).
func writeSteady(triggerPath, brightnessPath, brightness string) error {
	if err := writeSysfsFile(triggerPath, "none"); err != nil {
		return fmt.Errorf("ledsysfs: clearing trigger: %w", err)
	}
	if err := writeSysfsFile(brightnessPath, brightness); err != nil {
		return fmt.Errorf("ledsysfs: setting brightness: %w", err)
	}
	return nil
}

// writeTimer selects the "timer" trigger and sets its two periods. On real
// hardware, delay_on/delay_off only exist once "timer" has been written to
// trigger -- so the order here is not cosmetic.
func writeTimer(triggerPath, delayOnPath, delayOffPath, onMs, offMs string) error {
	if err := writeSysfsFile(triggerPath, "timer"); err != nil {
		return fmt.Errorf("ledsysfs: setting trigger: %w", err)
	}
	if err := writeSysfsFile(delayOnPath, onMs); err != nil {
		return fmt.Errorf("ledsysfs: setting delay_on: %w", err)
	}
	if err := writeSysfsFile(delayOffPath, offMs); err != nil {
		return fmt.Errorf("ledsysfs: setting delay_off: %w", err)
	}
	return nil
}

func writeSysfsFile(path, value string) error {
	return os.WriteFile(path, []byte(value+"\n"), 0o644)
}
