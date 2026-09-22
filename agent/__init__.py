"""Melder: Gerät-Seite (docs/spezifikation.md, Abschnitt 2).

Das einzige Programm auf der Basisstation, das mit der Cloud spricht. Liest
thermoctl ausschließlich über dessen vorhandene REST-Schnittstelle mit einem
eigenen, nur lesenden Token (`zone.read`, `device.read`, `audit.read`, künftig
`health.read` -- Abschnitt 10) und entscheidet **lokal**, welche Befehle es
überhaupt ausführt (Abschnitt 2: der Melder ist die Sicherheitsgrenze, nicht die
Cloud).
"""
