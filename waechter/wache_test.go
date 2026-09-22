package main

import "testing"

// Diese Tests bestätigen nur, dass jede Gerüst-Funktion tatsächlich
// fehlschlägt, statt etwas vorzutäuschen -- keine bestätigt eine bereits
// vorhandene Fähigkeit (die gibt es hier noch nicht).

func TestAgentGestopptNichtUmgesetzt(t *testing.T) {
	if _, err := AgentGestoppt(); err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestDigestStartenNichtUmgesetzt(t *testing.T) {
	if err := DigestStarten(Zustand{Gewuenscht: "sha256:9f2c"}); err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestGesundmeldungAbwartenNichtUmgesetzt(t *testing.T) {
	if _, err := GesundmeldungAbwarten(); err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestAufBewaehrtZuruecksetzenOhneBewaehrtenStand(t *testing.T) {
	if err := AufBewaehrtZuruecksetzen(Zustand{Gewuenscht: "sha256:9f2c"}); err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}

func TestAufBewaehrtZuruecksetzenNichtUmgesetzt(t *testing.T) {
	z := Zustand{Gewuenscht: "sha256:9f2c", Bewaehrt: "sha256:1a7b"}
	if err := AufBewaehrtZuruecksetzen(z); err == nil {
		t.Fatal("erwarteter Fehler blieb aus")
	}
}
