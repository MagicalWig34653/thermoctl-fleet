"""Einstiegspunkt des Melders (`python -m agent`).

Ruft noch keine echte Schleife auf -- die Bausteine in `agent/schleife.py` sind
Platzhalter (siehe deren Docstrings). Dieser Einstiegspunkt macht das beim Start
ausdrücklich, statt eine Schleife zu bauen, die sofort mit einem unklaren
Traceback abbricht.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "thermoctl-agent (Melder): nur das Gerüst ist vorhanden. "
        "Siehe docs/STATUS.md für den Umsetzungsstand.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover -- nur ein Einstiegspunkt, keine Logik
    sys.exit(main())
