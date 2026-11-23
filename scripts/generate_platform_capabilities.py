"""Regenerate the publishing section of docs/PLATFORM_CAPABILITIES.md from the code.

Usage: python scripts/generate_platform_capabilities.py [--check]

The section between the BEGIN and END markers is produced by
`runway.platforms.publish_capability.capability_document`, for the adapters a
live build registers (`LIVE_PUBLISHER_NAMES`). `--check` exits 1 when the
file is out of date (the test suite runs the same comparison).
"""

from __future__ import annotations

import sys
from pathlib import Path

from runway.platforms.publish_capability import capability_document
from runway.platforms.publisher_registry import LIVE_PUBLISHER_NAMES

DOCUMENT = Path(__file__).resolve().parents[1] / "docs" / "PLATFORM_CAPABILITIES.md"
BEGIN = "<!-- BEGIN generated: publishing capability -->"
END = "<!-- END generated: publishing capability -->"
CRLF, LF = chr(13) + chr(10), chr(10)


def render(current: str) -> str:
    section = f"{BEGIN}\n{capability_document(LIVE_PUBLISHER_NAMES)}{END}"
    if BEGIN in current and END in current:
        head, _, rest = current.partition(BEGIN)
        _, _, tail = rest.partition(END)
        return head + section + tail
    return current.rstrip("\n") + "\n\n" + section + "\n"


def main(argv: list[str]) -> int:
    # Compare with LF endings: a Windows checkout may have CRLF on disk.
    current = DOCUMENT.read_text(encoding="utf-8").replace(CRLF, LF)
    wanted = render(current)
    if "--check" in argv:
        if wanted != current:
            print(f"{DOCUMENT} is out of date; run {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    if wanted != current:
        DOCUMENT.write_text(wanted, encoding="utf-8", newline="\n")
        print(f"updated {DOCUMENT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
