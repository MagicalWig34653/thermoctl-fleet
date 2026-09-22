"""Protokollversion des Fleet-Vertrags.

Unabhängig von den Versionsnummern der beiden Abbilder (`fleet`, `agent`) und des
gesteuerten `thermoctl`. Wird erhöht, sobald sich ein Feldname, eine Pflichtangabe
oder die Bedeutung eines vorhandenen Feldes ändert — nicht bei rein additiven,
abwärtskompatiblen Erweiterungen. Der Melder sendet sie im Herzschlag nicht separat
mit; sie dient hier ausschließlich der Absprache zwischen den beiden Paketen dieses
Repositories und künftigen Verträglichkeitsprüfungen.
"""

PROTOKOLLVERSION = 1
