"""Storage keys: ``w/{workspace_id}/{asset_id}/{sha256[:16]}.{ext}`` (spec 02 §2).

Every store checks a key with :func:`parse_media_key` (the format, so a key can never name a
path outside the store's root) and with :func:`check_workspace` (the ``w/{id}/`` prefix must
be the caller's workspace) before touching storage. Identifiers are lowercase only, so two keys
can never differ by case alone and alias on a case-insensitive file system.

The extension is chosen by the server from :data:`KEY_EXTENSIONS`; it is a label for people
and URLs only. The probe never sees it (see :mod:`runway.mediastore.probe`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from runway.mediastore.errors import InvalidMediaKey, WorkspaceMismatch

_ID_PATTERN = r"[a-z0-9][a-z0-9_-]{0,63}"
_KEY_PATTERN = re.compile(
    rf"w/(?P<workspace>{_ID_PATTERN})/(?P<asset>{_ID_PATTERN})/"
    r"(?P<digest>[0-9a-f]{16})\.(?P<ext>[a-z0-9]{1,10})"
)
_ID_RE = re.compile(_ID_PATTERN)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# Device names Windows refuses as path components, with or without an extension.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
FALLBACK_EXTENSION = "bin"
_EXTENSION_BY_CONTENT_TYPE: Mapping[str, str] = MappingProxyType(
    {
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "video/webm": "webm",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }
)
KEY_EXTENSIONS = frozenset(_EXTENSION_BY_CONTENT_TYPE.values()) | {FALLBACK_EXTENSION}


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
    if not _ID_RE.fullmatch(text) or text in _WINDOWS_RESERVED:
        raise InvalidMediaKey(
            f"{what} is not a valid storage key component (lowercase letters, digits, - and _)"
        )
    return text


def normalize_sha256(value: str) -> str:
    digest = value.strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise InvalidMediaKey("sha256 must be 64 hexadecimal characters")
    return digest


def workspace_prefix(workspace_id: str | int) -> str:
    return f"w/{_check_id(workspace_id, 'workspace id')}/"


def key_extension(content_type: str) -> str:
    """Server-chosen key extension for a (validated) content type; ``bin`` when unknown."""
    return _EXTENSION_BY_CONTENT_TYPE.get(content_type.strip().lower(), FALLBACK_EXTENSION)


def make_media_key(
    workspace_id: str | int, asset_id: str | int, sha256: str, extension: str
) -> str:
    if extension not in KEY_EXTENSIONS:
        raise InvalidMediaKey(f"extension must be one of {sorted(KEY_EXTENSIONS)}")
    key = MediaKey(
        workspace_id=_check_id(workspace_id, "workspace id"),
        asset_id=_check_id(asset_id, "asset id"),
        digest_prefix=normalize_sha256(sha256)[:16],
        extension=extension,
    )
    return str(key)


def parse_media_key(key: str) -> MediaKey:
    match = _KEY_PATTERN.fullmatch(key)
    if match is None:
        raise InvalidMediaKey("storage key does not match w/{workspace}/{asset}/{hash}.{ext}")
    workspace = match["workspace"]
    asset = match["asset"]
    if workspace in _WINDOWS_RESERVED or asset in _WINDOWS_RESERVED:
        raise InvalidMediaKey("storage key uses a reserved name")
    if match["ext"] not in KEY_EXTENSIONS:
        raise InvalidMediaKey("storage key has an extension the server does not issue")
    return MediaKey(
        workspace_id=workspace,
        asset_id=asset,
        digest_prefix=match["digest"],
        extension=match["ext"],
    )


def check_workspace(key: str, workspace_id: str | int) -> MediaKey:
    """Parse ``key`` and refuse it unless it lies under ``w/{workspace_id}/``."""
    parsed = parse_media_key(key)
    if parsed.workspace_id != _check_id(workspace_id, "workspace id"):
        raise WorkspaceMismatch("storage key belongs to another workspace")
    return parsed
