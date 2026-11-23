"""Which code is running: the git revision of this checkout."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def checkout_revision() -> str:
    """Short (12-character) commit of this checkout, or "unknown".

    The launcher computes the same value (`git rev-parse --short=12 HEAD`) to
    tell whether a running API or worker is older code than the checkout.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else "unknown"
