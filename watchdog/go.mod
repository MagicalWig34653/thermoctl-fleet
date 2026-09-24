module github.com/magicalwig34653/thermoctl-fleet/waechter

// go.mod ohne eine einzige Abhaengigkeit -- ausdrueckliche Bedingung aus
// docs/spezifikation.md Abschnitt 18.3. Insbesondere kein Docker-SDK: die
// Containerlaufzeit wird ueber ihr Kommandozeilenwerkzeug oder ueber
// systemctl angesprochen, nicht ueber eine Bibliothek.
go 1.23
