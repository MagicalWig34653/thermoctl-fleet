"""Entry point of the agent (`python -m agent`).

Does not yet call a real loop -- the building blocks in `agent/loop.py` are
placeholders (see their docstrings). This entry point says so explicitly at
startup, instead of building a loop that immediately aborts with a confusing
traceback.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "thermoctl-agent: only the scaffold is in place. "
        "See docs/STATUS.md for the implementation status.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover -- just an entry point, no logic
    sys.exit(main())
