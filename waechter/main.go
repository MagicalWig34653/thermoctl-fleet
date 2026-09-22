// Einstiegspunkt des Wächters.
//
// -pruefmodus liest ausschließlich die Zustandsdatei und gibt sie aus, ohne
// irgendetwas zu starten oder zu stoppen -- das ist der Vertragstest aus
// Abschnitt 18.3 ("Python schreibt die Zustandsdatei, Go liest sie"), siehe
// pruefe_vertrag.sh. Der normale Modus ruft die Gerüst-Funktionen aus
// wache.go auf, die allesamt noch nicht umgesetzt sind.
package main

import (
	"flag"
	"fmt"
	"os"
)

func main() {
	datei := flag.String("datei", "", "Pfad zur Zustandsdatei")
	pruefmodus := flag.Bool("pruefmodus", false, "nur lesen und ausgeben, nichts starten/stoppen")
	flag.Parse()

	if *pruefmodus {
		os.Exit(pruefmodusAusfuehren(*datei))
		return
	}

	fmt.Fprintln(os.Stderr, "thermoctl-waechter: nur das Gerüst ist vorhanden. Siehe docs/STATUS.md für den Umsetzungsstand.")
	os.Exit(1)
}

// pruefmodusAusfuehren liest die Zustandsdatei und gibt sie zeilenbasiert auf
// stdout aus (GEWUENSCHT=..., BEWAEHRT=..., SEIT=...) -- absichtlich in
// Großbuchstaben, damit ein Vergleichsskript sie nicht mit einer
// Kommentarzeile der Quelldatei verwechseln kann.
func pruefmodusAusfuehren(datei string) int {
	if datei == "" {
		fmt.Fprintln(os.Stderr, "thermoctl-waechter: -datei fehlt")
		return 2
	}
	zustand, err := LadeZustand(datei)
	if err != nil {
		fmt.Fprintf(os.Stderr, "thermoctl-waechter: %v\n", err)
		return 2
	}
	fmt.Printf("GEWUENSCHT=%s\n", zustand.Gewuenscht)
	fmt.Printf("BEWAEHRT=%s\n", zustand.Bewaehrt)
	fmt.Printf("SEIT=%d\n", zustand.Seit)
	return 0
}
