// Der Vertrag zwischen Agent und Wächter: die Zustandsdatei (Abschnitt 17,
// Schritt 2; Abschnitt 18.3). Zeilenbasiert, KEIN JSON -- Absicht, nicht
// Vereinfachung: so ist der Vertrag in jeder Sprache mit Bordmitteln lesbar.
// Ein frisch ausgeliefertes Gerät startet nie mit leerem Bewaehrt: Das
// Abbild-Rezept trägt beim Bau den Digest der mitgelieferten Fassung als
// Gewuenscht und Bewaehrt ein (Abschnitt 22.5) -- ein leeres Bewaehrt ist
// damit ein Zeichen für eine fehlerhafte Auslieferung, kein Normalzustand.
package main

import (
	"fmt"
	"io"
	"os"
)

// Zustand ist der geparste Inhalt der Zustandsdatei. Gewuenscht: vom Agenten
// bereits geprüfter Digest (Abschnitt 13). Bewaehrt: Digest nach einer Stunde
// störungsfrei, beim Bau vorbelegt (Abschnitt 22.5). Seit: seit wann
// Gewuenscht gilt (Abschnitt 22.2, nachträglich festgelegt -- die einzige
// Lesart, mit der dieses Feld die Fristen aus Abschnitt 17 berechnen kann).
// EsimVorherigesProfil/EsimFrist: Rückfalluhr für eSIM-Profilwechsel
// (Abschnitt 24.4, nachträglich als zwei weitere Zeilen hier, keine eigene
// Datei). Leer/0, solange kein Wechsel aussteht.
type Zustand struct {
	Gewuenscht           string
	Bewaehrt             string
	Seit                 int64
	EsimVorherigesProfil string
	EsimFrist            int64
}

// ParseZustand liest eine Zustandsdatei aus r. Unbekannte Schlüssel werden
// ignoriert, nicht abgelehnt -- eine künftige, dem Wächter unbekannte Zeile
// darf ihn nicht am Lesen der bekannten hindern (Abschnitt 18.2, sinngemäß).
func ParseZustand(r io.Reader) (Zustand, error) {
	werte, err := liesSchluesselWertZeilen(r, "zustandsdatei")
	if err != nil {
		return Zustand{}, err
	}
	if werte["gewuenscht"] == "" {
		return Zustand{}, fmt.Errorf("zustandsdatei: 'gewuenscht' fehlt oder ist leer")
	}
	seit, err := parseOptionalerZeitstempel(werte["seit"], "seit", "zustandsdatei")
	if err != nil {
		return Zustand{}, err
	}
	esimFrist, err := parseOptionalerZeitstempel(werte["esim_frist"], "esim_frist", "zustandsdatei")
	if err != nil {
		return Zustand{}, err
	}
	return Zustand{
		Gewuenscht:           werte["gewuenscht"],
		Bewaehrt:             werte["bewaehrt"],
		Seit:                 seit,
		EsimVorherigesProfil: werte["esim_vorheriges_profil"],
		EsimFrist:            esimFrist,
	}, nil
}

// LadeZustand öffnet pfad und parst ihn. Kein Netz, keine Registry.
func LadeZustand(pfad string) (Zustand, error) {
	datei, err := os.Open(pfad)
	if err != nil {
		return Zustand{}, err
	}
	defer datei.Close()
	return ParseZustand(datei)
}
