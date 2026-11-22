"""Storage keys: ``w/{workspace_id}/{asset_id}/{sha256[:16]}.{ext}`` (spec 02 §2).

Every store validates keys with :func:`parse_media_key` before touching storage, so a key can
never name a path outside the workspace prefix, whatever the store does with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath

from runway.mediastore.errors import InvalidMediaKey

_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
_KEY_PATTERN = re.compile(
    rf"w/(?P<workspace>{_ID_PATTERN})/(?P<asset>{_ID_PATTERN})/"
    r"(?P<digest>[0-9a-f]{16})\.(?P<ext>[a-z0-9]{1,10})"
)
_ID_RE = re.compile(_ID_PATTERN)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EXT_RE = re.compile(r"[a-z0-9]{1,10}")
# Device names Windows refuses as path components, with or without an extension.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
FALLBACK_EXTENSION = "bin"


@dataclass(frozen=True)
class MediaKey:
    workspace_id: str
    asset_id: str
    digest_prefix: str
    extension: str

    def __str__(self) -> str:
        return f"w/{self.workspace_id}/{self.asset_id}/{self.digest_prefix}.{self.extension}"

    @property
    def workspace_prefix(self) -> str:
        return workspace_prefix(self.workspace_id)


def _check_id(value: str | int, what: str) -> str:
    text = str(value)
    if not _ID_RE.fullmatch(text) or text.lower() in _WINDOWS_RESERVED:
        raise InvalidMediaKey(f"{what} is not a valid storage key component")
    return text


def normalize_sha256(value: str) -> str:
    digest = value.strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise InvalidMediaKey("sha256 must be 64 hexadecimal characters")
    return digest


def workspace_prefix(workspace_id: str | int) -> str:
    return f"w/{_check_id(workspace_id, 'workspace id')}/"


def make_media_key(
    workspace_id: str | int, asset_id: str | int, sha256: str, extension: str
) -> str:
    ext = extension.strip().lstrip(".").lower()
    if not _EXT_RE.fullmatch(ext):
        raise InvalidMediaKey("extension must be 1-10 lowercase letters or digits")
    key = MediaKey(
        workspace_id=_check_id(workspace_id, "workspace id"),
        asset_id=_check_id(asset_id, "asset id"),
        digest_prefix=normalize_sha256(sha256)[:16],
        extension=ext,
    )
    return str(key)


def parse_media_key(key: str) -> MediaKey:
    match = _KEY_PATTERN.fullmatch(key)
    if match is None:
        raise InvalidMediaKey("storage key does not match w/{workspace}/{asset}/{hash}.{ext}")
    workspace = match["workspace"]
    asset = match["asset"]
    for component in (workspace, asset):
        if component.lower() in _WINDOWS_RESERVED:
            raise InvalidMediaKey("storage key uses a reserved name")
    return MediaKey(
        workspace_id=workspace,
        asset_id=asset,
        digest_prefix=match["digest"],
        extension=match["ext"],
    )


def safe_extension(filename: str | None) -> str:
    """Extension for a key, from the client's filename. Cosmetic only: the probe decides type."""
    if not filename:
        return FALLBACK_EXTENSION
    suffix = PurePath(filename.replace("\\", "/")).suffix.lstrip(".").lower()
    return suffix if _EXT_RE.fullmatch(suffix) else FALLBACK_EXTENSION
