"""Gemeinsame Datenmodelle für Fleet-Dienst und Melder.

Dieses Paket ist der Vertrag zwischen beiden Seiten (siehe README.md, Abschnitt
"Ein Repository, zwei Abbilder"). Alles, was zwischen `fleet` und `agent` über die
Leitung geht, ist hier als Pydantic-Modell festgehalten — nirgends sonst.

Bezug: docs/spezifikation.md. Feldnamen sind absichtlich deutsch und wörtlich aus
Abschnitt 5 (Herzschlag), 7 (Befehle), 13 (Sollzustand) und 4/15.3 (Anmeldung)
übernommen, damit ein Beispiel aus der Spezifikation unverändert als Testdatum
dient.
"""

from protokoll.anmeldung import Anmeldeanfrage, Anmeldebestaetigung, MelderAnmeldedatei
from protokoll.befehle import Befehl, BefehlErgebnis, BefehlTyp
from protokoll.ereignisse import Ereignis
from protokoll.herzschlag import (
    GeraeteZustand,
    Herzschlag,
    OffeneStoerung,
    RegelungsZustand,
    Stoerungsart,
    SystemZustand,
    ThermoctlZustand,
)
from protokoll.sollzustand import Aktualisierungsfenster, Dienste, DienstStand, Sollzustand
from protokoll.version import PROTOKOLLVERSION

__all__ = [
    "PROTOKOLLVERSION",
    "Aktualisierungsfenster",
    "Anmeldeanfrage",
    "Anmeldebestaetigung",
    "Befehl",
    "BefehlErgebnis",
    "BefehlTyp",
    "Dienste",
    "DienstStand",
    "Ereignis",
    "GeraeteZustand",
    "Herzschlag",
    "MelderAnmeldedatei",
    "OffeneStoerung",
    "RegelungsZustand",
    "Sollzustand",
    "Stoerungsart",
    "SystemZustand",
    "ThermoctlZustand",
]
