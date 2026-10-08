"""Write the API's OpenAPI schema to a JSON file.

The frontend generates its typed API client from this file (``npm run gen:api``).
Usage: ``poe openapi`` or ``python scripts/export_openapi.py [output-path]``.
Needs ``MONGO_URI`` (from the environment or ``.env``); the database is not contacted.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.main import create_app

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "openapi.json"


def main(argv: Sequence[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    schema = create_app().openapi()
    output.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"OpenAPI schema written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
