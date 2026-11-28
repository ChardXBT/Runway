"""Svix-style webhook signatures, as Clerk sends them.

Headers: `svix-id`, `svix-timestamp` (Unix seconds) and `svix-signature`, a
space-separated list of `v1,<base64 HMAC-SHA256>` entries. The signed content
is `<svix-id>.<svix-timestamp>.<raw body>`; the key is the base64 text after
the `whsec_` prefix of the endpoint's signing secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SECRET_PREFIX = "whsec_"
SIGNATURE_VERSION = "v1"
TOLERANCE_SECONDS = 5 * 60


class WebhookSignatureInvalid(Exception):
    """The delivery is not signed by the configured secret, or is too old or new."""


@dataclass(frozen=True, slots=True)
class SignedDelivery:
    delivery_id: str
    timestamp: int


def decode_secret(secret: str) -> bytes:
    material = secret.strip().removeprefix(SECRET_PREFIX)
    try:
        key = base64.b64decode(material, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("the webhook secret is not valid base64 after whsec_") from exc
    if not key:
        raise ValueError("the webhook secret is empty")
    return key


def sign(secret: str, delivery_id: str, timestamp: int, body: bytes) -> str:
    """Return a `v1,<signature>` entry; used by tests and by local rehearsals."""
    digest = _digest(decode_secret(secret), delivery_id, timestamp, body)
    return f"{SIGNATURE_VERSION},{base64.b64encode(digest).decode('ascii')}"


def _digest(key: bytes, delivery_id: str, timestamp: int, body: bytes) -> bytes:
    signed = delivery_id.encode("utf-8") + b"." + str(timestamp).encode("ascii") + b"." + body
    return hmac.new(key, signed, hashlib.sha256).digest()


def verify(
    secrets: Iterable[str],
    headers: Mapping[str, str],
    body: bytes,
    *,
    now: float | None = None,
    tolerance_seconds: int = TOLERANCE_SECONDS,
) -> SignedDelivery:
    """Return the verified delivery id and timestamp, or raise `WebhookSignatureInvalid`."""
    delivery_id = (headers.get("svix-id") or "").strip()
    raw_timestamp = (headers.get("svix-timestamp") or "").strip()
    raw_signatures = (headers.get("svix-signature") or "").strip()
    if not delivery_id or not raw_timestamp or not raw_signatures:
        raise WebhookSignatureInvalid("missing svix signature headers")
    if len(delivery_id) > 200:
        raise WebhookSignatureInvalid("svix-id is too long")
    if not raw_timestamp.isdigit():
        raise WebhookSignatureInvalid("svix-timestamp is not a number")
    timestamp = int(raw_timestamp)
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise WebhookSignatureInvalid("svix-timestamp is outside the tolerance")
    presented: list[bytes] = []
    for entry in raw_signatures.split():
        version, separator, value = entry.partition(",")
        if not separator or version != SIGNATURE_VERSION:
            continue
        try:
            presented.append(base64.b64decode(value, validate=True))
        except (binascii.Error, ValueError):
            continue
    if not presented:
        raise WebhookSignatureInvalid("no v1 signature")
    keys = [decode_secret(secret) for secret in secrets]
    if not keys:
        raise WebhookSignatureInvalid("no webhook secret is configured")
    matched = False
    for key in keys:
        expected = _digest(key, delivery_id, timestamp, body)
        for candidate in presented:
            # Constant-time comparison of the raw digest bytes; every pair is
            # compared so timing does not reveal which entry matched.
            matched |= hmac.compare_digest(expected, candidate)
    if not matched:
        raise WebhookSignatureInvalid("signature mismatch")
    return SignedDelivery(delivery_id=delivery_id, timestamp=timestamp)
