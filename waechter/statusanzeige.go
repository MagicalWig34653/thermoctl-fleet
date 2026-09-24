// Die Statusanzeige am Gerät: zwei LEDs am 40-poligen Anschluss, nur am
// Raspberry Pi vorhanden (Abschnitt 23). Angesteuert vom Wächter, nicht vom
// Agenten -- er läuft auch dann noch, wenn Container stehen oder das Netz
// weg ist, und genau dann will jemand vor Ort sehen, woran er ist.
//
// Ohne eine einzige Abhängigkeit (Abschnitt 23.1): kein GPIO-Paket, kein
// ioctl. Das Abbild-Rezept (Abschnitt 19) lädt die Kernel-Overlays
// `gpio-led`, danach schreibt der Wächter nur noch in zwei sysfs-Dateien.
package main

import (
	"fmt"
	"os"
)

// Pfade der beiden LED-Helligkeitsdateien, vom `gpio-led`-Overlay angelegt
// (Abschnitt 23.1) -- fest, weil sie an die GPIO-Nummern in `config.txt`
// gebunden sind, nicht an eine Konfiguration dieses Programms.
const (
	LedGeraetPfad = "/sys/class/leds/thermoctl-geraet/brightness"
	LedAnlagePfad = "/sys/class/leds/thermoctl-anlage/brightness"
)

// LedVorhanden prüft, ob eine der beiden Dateien existiert. Fehlt sie (kein
// Raspberry Pi, Overlay nicht geladen), ist das ausdrücklich **kein**
// Fehler -- die Anzeige ist optional, der Wächter arbeitet ohne sie
// unverändert weiter (Abschnitt 23.3).
func LedVorhanden(pfad string) bool {
	_, err := os.Stat(pfad)
	return err == nil
}

// LedMusterSetzen schreibt ein Blinkmuster für die LED unter pfad (Abschnitt
// 23.2: aus/an/langsames Blinken/schnelles Blinken/zweimal kurz), über den
// Kernel-Trigger `timer` (`delay_on`/`delay_off`), noch nicht umgesetzt --
// welcher Wächter-Zustand (AgentGestoppt, GesundmeldungAbwarten, ...) welches
// Muster auslöst, entsteht zusammen mit wache.go.
//
// Fehlt die Datei, wird nichts versucht und kein Fehler gemeldet (siehe
// LedVorhanden) -- ein Gerät ohne diesen Anschluss darf dadurch nicht
// blockiert werden.
func LedMusterSetzen(pfad string, muster string) error {
	if !LedVorhanden(pfad) {
		return nil
	}
	return fmt.Errorf("Ansteuern der LED %q (Muster %q) nicht umgesetzt -- siehe docs/spezifikation.md Abschnitt 23", pfad, muster)
}
