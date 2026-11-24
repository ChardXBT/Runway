#!/usr/bin/env python3
"""Write `apps/web/lib/v1/openapi.json` from the live `/api/v1` routes.

Offline: the FastAPI app is built in-process with a disposable data directory;
no server is started and no platform is contacted. Run via `npm run gen:api`
(which also regenerates the TypeScript types) or directly:

    .venv/Scripts/python.exe scripts/export_openapi.py [--check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from runway.api.v1.openapi_export import EXPORT_PATH, export_v1_openapi  # noqa: E402

OUT_PATH = REPO_ROOT / EXPORT_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the committed file instead of writing it",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write to this path instead of the committed export location",
    )
    args = parser.parse_args()

    rendered = export_v1_openapi()
    if args.check:
        committed = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if committed == rendered:
            print(f"{OUT_PATH.relative_to(REPO_ROOT)} is up to date.")
            return 0
        print(
            f"{OUT_PATH.relative_to(REPO_ROOT)} is stale or missing; "
            "run `npm run gen:api` to regenerate it.",
            file=sys.stderr,
        )
        return 1
    out_path = args.out or OUT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
