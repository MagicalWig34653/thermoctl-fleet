// Gerüst der eigentlichen Wächter-Entscheidungen (Abschnitt 17). Anders als
// zustand.go und gesundmeldung.go (reines, echtes Dateiparsen) braucht jede
// Funktion hier echten Zugriff auf die Containerlaufzeit oder systemd, den
// dieses Gerüst nicht vorwegnimmt -- jede gibt deshalb einen Fehler mit
// Verweis auf den zuständigen Schritt zurück statt etwas vorzutäuschen.
// Arbeitsteilung (Abschnitt 17, "Wer lädt, und wer tauscht"): Keine Funktion
// hier ruft eine Registry auf oder prüft einen Digest -- das hat der Agent
// bereits erledigt. Der Wächter kennt nur zwei lokal vorhandene Digests.
package main

import "fmt"

// AgentGestoppt erkennt, dass der Agent sich selbst gestoppt hat (Schritt 3).
// Der Agent tauscht sich nicht selbst aus -- er stoppt sich nur. Der Wächter
// muss das Ende bemerken, bevor er irgendetwas tut.
func AgentGestoppt() (bool, error) {
	return false, fmt.Errorf("Erkennen des Agent-Endes nicht umgesetzt -- siehe docs/spezifikation.md Abschnitt 17")
}

// DigestStarten startet den in z.Gewuenscht genannten Stand (Schritt 4).
// Kein Digest-Abgleich gegen eine Quellenliste hier -- siehe Modul-Docstring.
func DigestStarten(z Zustand) error {
	return fmt.Errorf("Starten des Digests %q nicht umgesetzt -- siehe docs/spezifikation.md Abschnitt 17", z.Gewuenscht)
}

// GesundmeldungAbwarten wartet auf die Gesundmeldung, höchstens 10 Minuten
// (Schritt 5), oder erkennt einen dreimaligen Neustart des Containers.
func GesundmeldungAbwarten() (bool, error) {
	return false, fmt.Errorf("Warten auf die Gesundmeldung nicht umgesetzt -- siehe docs/spezifikation.md Abschnitt 17")
}

// AufBewaehrtZuruecksetzen setzt bei Ausbleiben der Gesundmeldung oder nach
// drei Neustarts auf z.Bewaehrt zurück (Schritt 5). Ein leeres z.Bewaehrt ist
// seit Abschnitt 22.5 kein Normalzustand mehr: das Abbild-Rezept trägt es
// beim Bau vor, jedes ausgelieferte Gerät hat also ein Rückfallziel.
func AufBewaehrtZuruecksetzen(z Zustand) error {
	if z.Bewaehrt == "" {
		return fmt.Errorf("kein bewährter Digest vorhanden -- Anzeichen einer fehlerhaften Auslieferung, siehe docs/spezifikation.md Abschnitt 22.5")
	}
	return fmt.Errorf("Rückrollen auf %q nicht umgesetzt -- siehe docs/spezifikation.md Abschnitt 17", z.Bewaehrt)
}
