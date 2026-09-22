// Der Vertrag zwischen Agent und Wächter: die Zustandsdatei (Abschnitt 17,
// Schritt 2; Abschnitt 18.3).
//
// Zeilenbasiert, KEIN JSON -- das ist Absicht, nicht eine vorläufige
// Vereinfachung, und deshalb steht die Begründung genau hier, wo jemand sie
// beim "Aufräumen" sonst als überflüssig ansehen könnte: So ist der Vertrag
// in jeder Sprache mit Bordmitteln lesbar -- in Go, in Rust ohne
// Fremdpakete, in Python, notfalls in drei Zeilen Shell. Die Sprachwahl des
// Wächters (heute Go) bleibt damit später revidierbar, ohne den Vertrag
// selbst zu brechen. Wer dieses Format durch JSON ersetzt, nimmt diese
// Eigenschaft weg -- bitte vorher mit dem Projektinhaber klären, nicht
// still tauschen.
package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

// Zustand ist der geparste Inhalt der Zustandsdatei.
//
// Gewuenscht: Digest, den der Agent nach einem Sollzustandsabgleich als Ziel
// abgelegt hat (Abschnitt 17, Schritt 2) -- der Agent hat ihn zu diesem
// Zeitpunkt bereits gegen die fest eingebauten Quellen geprüft (Abschnitt
// 13); der Wächter prüft hier nichts mehr nach, er tauscht nur (Abschnitt
// 17, "Wer lädt, und wer tauscht").
//
// Bewaehrt: Digest, der nach einer Stunde störungsfreien Betriebs vom Agenten
// selbst als sicherer Rückfallpunkt fortgeschrieben wurde (Schritt 6). Leer,
// solange noch keine Fassung diese Stunde überstanden hat -- ein frisch
// eingerichtetes Gerät hat noch keinen bewährten Stand (siehe
// docs/STATUS.md, offene Punkte).
//
// Seit: Unix-Sekunden. Die Spezifikation zeigt dieses Feld nur an einem
// Beispiel, ohne seine genaue Bedeutung textlich festzulegen. Hier als der
// Zeitpunkt behandelt, seit dem Gewuenscht der aktuelle Zielstand ist --
// diese Lesart ist eine Annahme des Gerüsts, keine belegte Festlegung; vor
// der echten Umsetzung von wache.go mit dem Projektinhaber klären (siehe
// docs/STATUS.md).
type Zustand struct {
	Gewuenscht string
	Bewaehrt   string
	Seit       int64
}

// ParseZustand liest eine Zustandsdatei aus r. Unbekannte Schlüssel werden
// ignoriert, nicht abgelehnt -- Abschnitt 18.2 legt für den Herzschlag fest
// "ein Feld darf nur hinzukommen", derselbe Gedanke gilt hier: eine künftige,
// dem Wächter unbekannte Zeile darf ihn nicht am Lesen der bekannten hindern.
func ParseZustand(r io.Reader) (Zustand, error) {
	var z Zustand
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		zeile := strings.TrimSpace(scanner.Text())
		if zeile == "" || strings.HasPrefix(zeile, "#") {
			continue
		}
		schluessel, wert, gefunden := strings.Cut(zeile, "=")
		if !gefunden {
			return Zustand{}, fmt.Errorf("zustandsdatei: Zeile ohne '=': %q", zeile)
		}
		wert = strings.TrimSpace(wert)
		switch strings.TrimSpace(schluessel) {
		case "gewuenscht":
			z.Gewuenscht = wert
		case "bewaehrt":
			z.Bewaehrt = wert
		case "seit":
			if wert == "" {
				continue
			}
			seit, err := strconv.ParseInt(wert, 10, 64)
			if err != nil {
				return Zustand{}, fmt.Errorf("zustandsdatei: 'seit' ist kein Unix-Zeitstempel: %w", err)
			}
			z.Seit = seit
		}
	}
	if err := scanner.Err(); err != nil {
		return Zustand{}, err
	}
	if z.Gewuenscht == "" {
		return Zustand{}, fmt.Errorf("zustandsdatei: 'gewuenscht' fehlt oder ist leer")
	}
	return z, nil
}

// LadeZustand öffnet pfad und parst ihn. Kein Netz, keine Registry -- eine
// reine Dateioperation (Abschnitt 17, "Wer lädt, und wer tauscht").
func LadeZustand(pfad string) (Zustand, error) {
	datei, err := os.Open(pfad)
	if err != nil {
		return Zustand{}, err
	}
	defer datei.Close()
	return ParseZustand(datei)
}
