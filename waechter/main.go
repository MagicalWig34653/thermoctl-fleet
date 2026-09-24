// Einstiegspunkt des Wächters. -pruefmodus liest Zustands- und (optional)
// Gesundmeldungsdatei und gibt beide aus -- der Vertragstest aus Abschnitt
// 18.3, siehe pruefe_vertrag.sh. Sonst: die Gerüst-Funktionen aus wache.go.
package main

import (
	"flag"
	"fmt"
	"os"
)

func main() {
	datei := flag.String("datei", "", "Pfad zur Zustandsdatei")
	gesundmeldungsdatei := flag.String("gesundmeldungsdatei", "", "Pfad zur Gesundmeldungsdatei (optional)")
	pruefmodus := flag.Bool("pruefmodus", false, "nur lesen und ausgeben")
	flag.Parse()

	if *pruefmodus {
		os.Exit(pruefmodusAusfuehren(*datei, *gesundmeldungsdatei))
	}
	fmt.Fprintln(os.Stderr, "thermoctl-waechter: nur das Gerüst ist vorhanden. Siehe docs/STATUS.md für den Umsetzungsstand.")
	os.Exit(1)
}

// pruefmodusAusfuehren gibt beide Dateien großbuchstabig aus (Vergleichsskript).
func pruefmodusAusfuehren(datei, gesundmeldungsdatei string) int {
	if datei == "" {
		fmt.Fprintln(os.Stderr, "thermoctl-waechter: -datei fehlt")
		return 2
	}
	zustand, err := LadeZustand(datei)
	if err != nil {
		return meldeFehler(err)
	}
	fmt.Printf("GEWUENSCHT=%s\nBEWAEHRT=%s\nSEIT=%d\nESIM_VORHERIGES_PROFIL=%s\nESIM_FRIST=%d\n",
		zustand.Gewuenscht, zustand.Bewaehrt, zustand.Seit, zustand.EsimVorherigesProfil, zustand.EsimFrist)
	if gesundmeldungsdatei == "" {
		return 0
	}
	gesundmeldung, err := LiesGesundmeldung(gesundmeldungsdatei)
	if err != nil {
		return meldeFehler(err)
	}
	fmt.Printf("ZM_ZEITPUNKT=%d\nZM_DIGEST=%s\nZM_FASSUNG=%s\n",
		gesundmeldung.Zeitpunkt, gesundmeldung.Digest, gesundmeldung.Fassung)
	return 0
}

func meldeFehler(err error) int {
	fmt.Fprintf(os.Stderr, "thermoctl-waechter: %v\n", err)
	return 2
}
