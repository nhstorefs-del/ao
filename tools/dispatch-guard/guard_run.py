#!/usr/bin/env python3
"""Legacy arbitrary-command runner disabled by DELEGATE-FIRST enforcement.

Use AO configured worker dispatch (ao_run.py) for executable work. Standalone
snapshot.py/diff_guard.py remain available for independent artifact inspection.
This wrapper cannot bind arbitrary argv to a verified worker execution identity.
"""
from __future__ import annotations
import sys


def main() -> int:
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    print("DELEGATION_GATE_REFUSED: UNBOUNDED_COMMAND_ROUTE_DISABLED; use ao_run.py configured worker dispatch", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
