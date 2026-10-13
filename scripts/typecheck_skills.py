"""Type-check skill scripts one at a time.

They live in folders that are not Python packages (``skills/<name>/scripts/``) and share file
names (``static.py``), so mypy cannot take them in its normal run (they are excluded there).
Run by ``poe typecheck``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from mypy import api

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    scripts = sorted(ROOT.glob("app/**/skills/*/scripts/*.py"))
    failed = 0
    for script in scripts:
        output, errors, status = api.run([str(script)])
        if status:
            failed = 1
            sys.stdout.write(output + errors)
    sys.stdout.write(f"skill scripts: {len(scripts)} checked, {'FAILED' if failed else 'ok'}\n")
    return failed


if __name__ == "__main__":
    sys.exit(main())
