"""Fleet-Dienst: Cloud-Seite (docs/spezifikation.md, Abschnitt 2).

Empfängt Herzschläge und Ereignisse, gibt Befehle über einen SSE-Strom aus, hält
Sollzustände vor. Sieht **keine** Raumtemperaturen, Sollwerte oder Mieterdaten
(Abschnitt 6) und kann die Heizung nicht regeln (Abschnitt 1) -- das ist kein
Implementierungsdetail, sondern der Zuschnitt, den dieses Repository umsetzt.
"""
