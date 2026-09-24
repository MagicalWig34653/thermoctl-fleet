package main

import (
	"strings"
	"testing"
)

func TestParseZustandVollstaendig(t *testing.T) {
	eingabe := "gewuenscht=sha256:9f2c\nbewaehrt=sha256:1a7b\nseit=1790000123\n"

	z, err := ParseZustand(strings.NewReader(eingabe))
	if err != nil {
		t.Fatalf("unerwarteter Fehler: %v", err)
	}
	if z.Gewuenscht != "sha256:9f2c" {
		t.Errorf("Gewuenscht = %q, erwartet sha256:9f2c", z.Gewuenscht)
	}
	if z.Bewaehrt != "sha256:1a7b" {
		t.Errorf("Bewaehrt = %q, erwartet sha256:1a7b", z.Bewaehrt)
	}
	if z.Seit != 1790000123 {
		t.Errorf("Seit = %d, erwartet 1790000123", z.Seit)
	}
}

func TestParseZustandOhneBewaehrt(t *testing.T) {
	// Ein frisch eingerichtetes Gerät hat noch keinen bewährten Stand
	// (siehe docs/STATUS.md) -- die Zeile fehlt hier ganz.
	eingabe := "gewuenscht=sha256:9f2c\nseit=1790000123\n"

	z, err := ParseZustand(strings.NewReader(eingabe))
	if err != nil {
		t.Fatalf("unerwarteter Fehler: %v", err)
	}
	if z.Bewaehrt != "" {
		t.Errorf("Bewaehrt = %q, erwartet leer", z.Bewaehrt)
	}
}

func TestParseZustandOhneGewuenschtWirdAbgelehnt(t *testing.T) {
	eingabe := "bewaehrt=sha256:1a7b\nseit=1790000123\n"

	_, err := ParseZustand(strings.NewReader(eingabe))
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestParseZustandLehntZeileOhneGleichheitszeichenAb(t *testing.T) {
	eingabe := "gewuenscht=sha256:9f2c\netwas ohne Gleichheitszeichen\n"

	_, err := ParseZustand(strings.NewReader(eingabe))
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestParseZustandLehntUngueltigenZeitstempelAb(t *testing.T) {
	eingabe := "gewuenscht=sha256:9f2c\nseit=nicht-eine-zahl\n"

	_, err := ParseZustand(strings.NewReader(eingabe))
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestParseZustandIgnoriertKommentareUndUnbekannteSchluessel(t *testing.T) {
	// Abschnitt 18.2, sinngemäß übernommen: ein neues Feld darf den Wächter
	// nicht am Lesen der bekannten Felder hindern.
	eingabe := "# Kommentar\ngewuenscht=sha256:9f2c\nkuenftiges_feld=irgendwas\n"

	z, err := ParseZustand(strings.NewReader(eingabe))
	if err != nil {
		t.Fatalf("unerwarteter Fehler: %v", err)
	}
	if z.Gewuenscht != "sha256:9f2c" {
		t.Errorf("Gewuenscht = %q, erwartet sha256:9f2c", z.Gewuenscht)
	}
}

func TestLadeZustandFehltDatei(t *testing.T) {
	_, err := LadeZustand("/pfad/der/nicht/existiert.env")
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestParseZustandLiestEsimRueckfallzeilen(t *testing.T) {
	// Abschnitt 24.4, nachträglich festgelegt: die Rückfalluhr für einen
	// eSIM-Profilwechsel steht als zwei weitere Zeilen in derselben Datei.
	eingabe := "gewuenscht=sha256:9f2c\nesim_vorheriges_profil=profil-1\nesim_frist=1790000723\n"

	z, err := ParseZustand(strings.NewReader(eingabe))
	if err != nil {
		t.Fatalf("unerwarteter Fehler: %v", err)
	}
	if z.EsimVorherigesProfil != "profil-1" {
		t.Errorf("EsimVorherigesProfil = %q, erwartet profil-1", z.EsimVorherigesProfil)
	}
	if z.EsimFrist != 1790000723 {
		t.Errorf("EsimFrist = %d, erwartet 1790000723", z.EsimFrist)
	}
}

func TestParseZustandOhneEsimZeilenBleibtLeer(t *testing.T) {
	// Kein Profilwechsel aussteht -- keine der beiden Zeilen vorhanden.
	eingabe := "gewuenscht=sha256:9f2c\n"

	z, err := ParseZustand(strings.NewReader(eingabe))
	if err != nil {
		t.Fatalf("unerwarteter Fehler: %v", err)
	}
	if z.EsimVorherigesProfil != "" || z.EsimFrist != 0 {
		t.Errorf("Esim-Felder = %q/%d, erwartet leer/0", z.EsimVorherigesProfil, z.EsimFrist)
	}
}

func TestParseZustandLehntUngueltigeEsimFristAb(t *testing.T) {
	eingabe := "gewuenscht=sha256:9f2c\nesim_frist=nicht-eine-zahl\n"

	_, err := ParseZustand(strings.NewReader(eingabe))
	if err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}
