package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLiesGesundmeldungVollstaendig(t *testing.T) {
	pfad := filepath.Join(t.TempDir(), "gesundmeldung")
	inhalt := "zeitpunkt=1790000123\ndigest=sha256:9f2c\nfassung=0.4.0\n"
	if err := os.WriteFile(pfad, []byte(inhalt), 0o600); err != nil {
		t.Fatalf("Vorbereitung fehlgeschlagen: %v", err)
	}

	g, err := LiesGesundmeldung(pfad)
	if err != nil {
		t.Fatalf("unerwarteter Fehler: %v", err)
	}
	if g.Zeitpunkt != 1790000123 {
		t.Errorf("Zeitpunkt = %d, erwartet 1790000123", g.Zeitpunkt)
	}
	if g.Digest != "sha256:9f2c" {
		t.Errorf("Digest = %q, erwartet sha256:9f2c", g.Digest)
	}
	if g.Fassung != "0.4.0" {
		t.Errorf("Fassung = %q, erwartet 0.4.0", g.Fassung)
	}
}

func TestParseGesundmeldungLehntUngueltigenZeitstempelAb(t *testing.T) {
	_, err := ParseGesundmeldung(strings.NewReader("zeitpunkt=kein-zeitstempel\n"))
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestParseGesundmeldungLehntFehlendenZeitpunktAb(t *testing.T) {
	_, err := ParseGesundmeldung(strings.NewReader("digest=sha256:9f2c\nfassung=0.4.0\n"))
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestParseGesundmeldungLehntZeileOhneGleichheitszeichenAb(t *testing.T) {
	_, err := ParseGesundmeldung(strings.NewReader("zeitpunkt=1790000123\netwas ohne Gleichheitszeichen\n"))
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestParseGesundmeldungIgnoriertUnbekannteSchluessel(t *testing.T) {
	// Wie bei der Zustandsdatei: ein künftiges Feld darf das Lesen der
	// bekannten Felder nicht verhindern.
	g, err := ParseGesundmeldung(strings.NewReader("zeitpunkt=1790000123\nkuenftiges_feld=irgendwas\n"))
	if err != nil {
		t.Fatalf("unerwarteter Fehler: %v", err)
	}
	if g.Zeitpunkt != 1790000123 {
		t.Errorf("Zeitpunkt = %d, erwartet 1790000123", g.Zeitpunkt)
	}
}

func TestLiesGesundmeldungFehltDatei(t *testing.T) {
	_, err := LiesGesundmeldung(filepath.Join(t.TempDir(), "fehlt"))
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}
