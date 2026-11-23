"""The release-bound payload and its hash (spec 03 section 2).

A release approves "this exact post". The hash covers what reaches the
platform: the caption, the effective settings (defaults applied), the media
bytes (by SHA-256, in order), the account's external id and the planned
time, plus the platform and post type. Editing any of them changes the hash,
which clears the release; the worker recomputes it before sending and
refuses a mismatch.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

PAYLOAD_VERSION = 1


def _instant(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_payload(
    *,
    platform: str,
    post_type: str | None,
    account_external_id: str | None,
    caption: str,
    settings: Mapping[str, Any],
    media_sha256: Sequence[str],
    planned_at: datetime | None,
) -> dict[str, Any]:
    return {
        "v": PAYLOAD_VERSION,
        "platform": platform,
        "post_type": post_type,
        "account_external_id": account_external_id,
        "caption": caption,
        "settings": dict(settings),
        "media_sha256": [value.lower() for value in media_sha256],
        "planned_at": _instant(planned_at),
    }


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 (hex) of the canonical JSON of `payload`."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
