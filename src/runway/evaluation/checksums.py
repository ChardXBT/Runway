from __future__ import annotations

import hashlib
from pathlib import Path


def canonical_text_sha256(path: Path) -> str:
    """Hash text with the frozen baseline's canonical CRLF line endings."""
    raw = path.read_bytes()
    lf_normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    canonical = lf_normalized.replace(b"\n", b"\r\n")
    return hashlib.sha256(canonical).hexdigest()
