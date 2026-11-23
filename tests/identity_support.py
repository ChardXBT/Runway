"""Test-only RSA keys, JWKS documents, Clerk-shaped session tokens and Svix signatures.

Nothing here touches the network: tests hand `jwks_fetcher(...)` to the
provider in place of the HTTP fetcher.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from runway.identity import svix

ISSUER = "https://clerk.runway.test"
WEB_ORIGIN = "https://app.runway.test"
# Base64 of 32 fixed bytes; a test value, not a real secret.
WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"runway-test-webhook-secret-0001!").decode("ascii")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@lru_cache(maxsize=8)
def rsa_key(name: str = "primary", bits: int = 2048) -> rsa.RSAPrivateKey:
    """A key per name, generated once per test session."""
    del name
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


def jwks(*entries: tuple[rsa.RSAPrivateKey, str]) -> dict[str, Any]:
    return {"keys": [jwk(key, kid) for key, kid in entries]}


class FakeJwksFetcher:
    """Serves a JWKS document and counts how often it was asked."""

    def __init__(self, document: Mapping[str, Any] | None = None) -> None:
        self.document: Mapping[str, Any] | None = document
        self.calls = 0
        self.error: Exception | None = None

    def __call__(self, url: str) -> Mapping[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.document is None:
            raise ConnectionError("no JWKS document configured")
        return self.document


def mint(
    key: rsa.RSAPrivateKey,
    claims: Mapping[str, Any],
    *,
    kid: str = "kid-1",
    header: Mapping[str, Any] | None = None,
) -> str:
    head = {"alg": "RS256", "typ": "JWT", "kid": kid, **(header or {})}
    signing_input = (
        b64url(json.dumps(head).encode("utf-8"))
        + "."
        + b64url(json.dumps(dict(claims)).encode("utf-8"))
    )
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64url(signature)}"


def session_claims(
    user_id: str,
    org_id: str | None = None,
    *,
    role: str = "admin",
    form: str = "v2",
    issuer: str = ISSUER,
    azp: str | None = WEB_ORIGIN,
    lifetime: int = 60,
    now: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Claims shaped like a Clerk session token (v2 `o` claim or v1 `org_*` claims)."""
    issued = int(time.time() if now is None else now)
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": user_id,
        "sid": f"sess_{user_id}",
        "iat": issued,
        "nbf": issued - 1,
        "exp": issued + lifetime,
    }
    if azp is not None:
        claims["azp"] = azp
    if org_id is not None:
        if form == "v2":
            claims["v"] = 2
            claims["sts"] = "active"
            claims["o"] = {"id": org_id, "rol": role, "slg": org_id.removeprefix("org_")}
        else:
            claims["org_id"] = org_id
            claims["org_role"] = f"org:{role}"
            claims["org_slug"] = org_id.removeprefix("org_")
    claims.update(extra)
    return claims


def svix_headers(
    body: bytes,
    delivery_id: str,
    *,
    secret: str = WEBHOOK_SECRET,
    timestamp: int | None = None,
) -> dict[str, str]:
    stamp = int(time.time()) if timestamp is None else timestamp
    return {
        "svix-id": delivery_id,
        "svix-timestamp": str(stamp),
        "svix-signature": svix.sign(secret, delivery_id, stamp, body),
        "content-type": "application/json",
    }


def membership_event(
    kind: str,
    org_id: str,
    user_id: str,
    *,
    role: str = "org:member",
    timestamp_ms: int | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    return {
        "object": "event",
        "type": f"organizationMembership.{kind}",
        "timestamp": timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
        "data": {
            "id": f"orgmem_{org_id}_{user_id}",
            "object": "organization_membership",
            "role": role,
            "organization": {"id": org_id, "name": f"Org {org_id}", "slug": org_id},
            "public_user_data": {
                "user_id": user_id,
                "identifier": email or f"{user_id}@example.test",
                "first_name": "Test",
                "last_name": user_id,
            },
        },
    }
