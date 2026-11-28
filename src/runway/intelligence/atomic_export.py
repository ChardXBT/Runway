from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_text_atomically(destination: Path, content: str, *, temporary_prefix: str) -> None:
    """Write text through a sibling temporary file and atomically rename it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=temporary_prefix,
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
