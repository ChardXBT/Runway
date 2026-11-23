"""RS256 JSON Web Token verification against a cached JWKS, using `cryptography`.

Only RS256 is accepted, so the algorithm-confusion attacks of general JWT
libraries (`alg: none`, HS256 signed with the public key) cannot arise: the
header's `alg` must be exactly RS256 and the key must be an RSA key of at least
2048 bits. Claims are checked by the caller's `ClaimRules`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from runway.identity.base import IdentityRejected, IdentityUnavailable

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"
MINIMUM_RSA_BITS = 2048
# A session token is a few kilobytes; anything far larger is refused unparsed.
MAXIMUM_TOKEN_LENGTH = 16 * 1024

JwksFetcher = Callable[[str], Mapping[str, Any]]
Clock = Callable[[], float]


def b64url_decode(segment: str) -> bytes:
    if not segment or any(character in segment for character in "=+/ \t\r\n"):
        raise IdentityRejected("invalid_token", "the token is not a compact JWT")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise IdentityRejected("invalid_token", "the token is not a compact JWT") from exc


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityRejected("invalid_token", "the token is not a compact JWT") from exc
    if not isinstance(value, dict):
        raise IdentityRejected("invalid_token", "the token is not a compact JWT")
    return value


def _int_from_b64url(segment: object) -> int:
    if not isinstance(segment, str):
        raise ValueError("JWK integer is not a string")
    return int.from_bytes(b64url_decode(segment), "big")


def rsa_public_key_from_jwk(jwk: Mapping[str, Any]) -> rsa.RSAPublicKey:
    """Build an RSA public key from a JWK; refuse anything but a strong RS256 signing key."""
    if jwk.get("kty") != "RSA":
        raise ValueError("not an RSA key")
    if jwk.get("use") not in (None, "sig"):
        raise ValueError("not a signing key")
    if jwk.get("alg") not in (None, ALGORITHM):
        raise ValueError("not an RS256 key")
    try:
        modulus = _int_from_b64url(jwk.get("n"))
        exponent = _int_from_b64url(jwk.get("e"))
    except IdentityRejected as exc:
        raise ValueError("malformed RSA key") from exc
    key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
    if key.key_size < MINIMUM_RSA_BITS:
        raise ValueError("RSA key is too small")
    return key


def httpx_jwks_fetcher(url: str) -> Mapping[str, Any]:
    """Default fetcher: a plain GET with a short timeout and no redirects."""
    response = httpx.get(url, timeout=5.0, follow_redirects=False)
    response.raise_for_status()
    payload: object = response.json()
    if not isinstance(payload, dict):
        raise ValueError("JWKS response is not an object")
    return payload


@dataclass
class JwksCache:
    """In-memory key cache keyed by `kid`.

    Keys are reused for `ttl_seconds`. A token with an unknown `kid` (or stale
    keys) causes at most one fetch per `min_refresh_seconds`, so a flood of
    forged tokens cannot become a flood of requests to the provider. When a
    refresh fails the previously fetched keys keep being used.
    """

    url: str
    fetcher: JwksFetcher = httpx_jwks_fetcher
    clock: Clock = time.monotonic
    ttl_seconds: float = 3600.0
    min_refresh_seconds: float = 60.0
    _keys: dict[str, rsa.RSAPublicKey] = field(default_factory=dict, init=False)
    _fetched_at: float | None = field(default=None, init=False)
    _attempted_at: float | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def key_for(self, kid: str) -> rsa.RSAPublicKey:
        with self._lock:
            now = self.clock()
            key = self._keys.get(kid)
            stale = self._fetched_at is None or now - self._fetched_at >= self.ttl_seconds
            if (key is None or stale) and self._may_attempt(now):
                self._refresh(now)
                key = self._keys.get(kid)
            have_keys = bool(self._keys)
        if key is not None:
            return key
        if not have_keys:
            raise IdentityUnavailable("the identity provider's keys are unavailable")
        raise IdentityRejected("invalid_token", "the token was signed with an unknown key")

    def _may_attempt(self, now: float) -> bool:
        return self._attempted_at is None or now - self._attempted_at >= self.min_refresh_seconds

    def _refresh(self, now: float) -> None:
        self._attempted_at = now
        try:
            keys = _parse_jwks(self.fetcher(self.url))
        except Exception as exc:  # noqa: BLE001 - every fetch failure is handled the same way
            logger.warning(
                "identity provider JWKS refresh failed: %s",
                type(exc).__name__,
                extra={"event_type": "identity_jwks_refresh_failed"},
            )
            return
        self._keys = keys
        self._fetched_at = now


def _parse_jwks(payload: Mapping[str, Any]) -> dict[str, rsa.RSAPublicKey]:
    entries = payload.get("keys")
    if not isinstance(entries, list):
        raise ValueError("JWKS has no keys list")
    keys: dict[str, rsa.RSAPublicKey] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        try:
            keys[kid] = rsa_public_key_from_jwk(entry)
        except ValueError:
            continue
    if not keys:
        raise ValueError("JWKS has no usable RS256 keys")
    return keys


@dataclass(frozen=True)
class ClaimRules:
    """What a verified token's registered claims must satisfy."""

    issuer: str
    authorized_parties: frozenset[str] = frozenset()
    audience: str | None = None
    leeway_seconds: int = 5
    require_azp: bool = False


def verify_rs256(
    token: str,
    keys: JwksCache,
    rules: ClaimRules,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify the signature and registered claims; return the claims.

    Raises `IdentityRejected` for any token that is malformed, forged, expired,
    not yet valid, from another issuer or for another party.
    """
    if len(token) > MAXIMUM_TOKEN_LENGTH:
        raise IdentityRejected("invalid_token", "the token is too large")
    parts = token.split(".")
    if len(parts) != 3:
        raise IdentityRejected("invalid_token", "the token is not a compact JWT")
    header_segment, payload_segment, signature_segment = parts
    header = _json_object(b64url_decode(header_segment))
    if header.get("alg") != ALGORITHM:
        raise IdentityRejected("invalid_token", "the token is not signed with RS256")
    if "crit" in header:
        raise IdentityRejected("invalid_token", "the token uses unsupported header extensions")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise IdentityRejected("invalid_token", "the token names no signing key")
    signature = b64url_decode(signature_segment)
    key = keys.key_for(kid)
    try:
        key.verify(
            signature,
            f"{header_segment}.{payload_segment}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise IdentityRejected("invalid_token", "the token signature is not valid") from exc
    claims = _json_object(b64url_decode(payload_segment))
    _check_claims(claims, rules, time.time() if now is None else now)
    return claims


def _numeric(claims: Mapping[str, Any], name: str, *, required: bool) -> float | None:
    value = claims.get(name)
    if value is None:
        if required:
            raise IdentityRejected("invalid_token", f"the token has no {name} claim")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IdentityRejected("invalid_token", f"the token's {name} claim is not a number")
    return float(value)


def _check_claims(claims: Mapping[str, Any], rules: ClaimRules, now: float) -> None:
    if claims.get("iss") != rules.issuer:
        raise IdentityRejected("invalid_token", "the token was issued by another issuer")
    leeway = rules.leeway_seconds
    expires = _numeric(claims, "exp", required=True)
    if expires is not None and now - leeway >= expires:
        raise IdentityRejected("token_expired", "the token has expired")
    not_before = _numeric(claims, "nbf", required=False)
    if not_before is not None and now + leeway < not_before:
        raise IdentityRejected("invalid_token", "the token is not valid yet")
    issued_at = _numeric(claims, "iat", required=False)
    if issued_at is not None and now + leeway < issued_at:
        raise IdentityRejected("invalid_token", "the token was issued in the future")
    azp = claims.get("azp")
    if azp is None:
        if rules.require_azp:
            raise IdentityRejected("invalid_token", "the token names no authorized party")
    elif not isinstance(azp, str) or _origin_key(azp) not in rules.authorized_parties:
        raise IdentityRejected("invalid_token", "the token was issued for another origin")
    if rules.audience is not None:
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if rules.audience not in audiences:
            raise IdentityRejected("invalid_token", "the token was issued for another audience")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise IdentityRejected("invalid_token", "the token has no subject")


def _origin_key(origin: str) -> str:
    return origin.strip().rstrip("/").casefold()


def normalized_origins(origins: object) -> frozenset[str]:
    if not isinstance(origins, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(_origin_key(str(origin)) for origin in origins if str(origin).strip())
