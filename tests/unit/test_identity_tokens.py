"""RS256 verification, the JWKS cache, Clerk claims and Svix signatures (no network)."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from identity_support import (
    ISSUER,
    WEB_ORIGIN,
    WEBHOOK_SECRET,
    FakeJwksFetcher,
    b64url,
    jwks,
    membership_event,
    mint,
    rsa_key,
    session_claims,
    svix_headers,
)
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.requests import Request

from runway.config import Settings
from runway.identity import svix
from runway.identity.base import (
    IdentityRejected,
    IdentityUnavailable,
    presented_credential,
    presents_api_key,
)
from runway.identity.clerk import ClerkConfig, ClerkIdentityProvider, organization_claims
from runway.identity.clerk_webhooks import WebhookPayloadInvalid, parse_clerk_event
from runway.identity.events import MembershipChanged, MembershipRemoved, UserChanged
from runway.identity.jwt import ClaimRules, JwksCache, rsa_public_key_from_jwk, verify_rs256
from runway.identity.registry import build_identity_provider

RULES = ClaimRules(issuer=ISSUER, authorized_parties=frozenset({WEB_ORIGIN}))


def _cache(fetcher: FakeJwksFetcher, clock: list[float] | None = None) -> JwksCache:
    ticks = clock if clock is not None else [0.0]
    return JwksCache(
        url="https://clerk.runway.test/.well-known/jwks.json",
        fetcher=fetcher,
        clock=lambda: ticks[0],
    )


def _keys() -> JwksCache:
    return _cache(FakeJwksFetcher(jwks((rsa_key(), "kid-1"))))


def test_a_valid_token_verifies_and_returns_its_claims() -> None:
    claims = verify_rs256(mint(rsa_key(), session_claims("user_1", "org_a")), _keys(), RULES)
    assert claims["sub"] == "user_1"
    assert claims["o"]["id"] == "org_a"


@pytest.mark.parametrize(
    ("make_claims", "code"),
    [
        # Factories, so the claims are minted when the test runs, not when it is
        # collected (a collected 60 s token can expire before a late test runs).
        (lambda: session_claims("user_1", lifetime=-30), "token_expired"),
        (lambda: session_claims("user_1", issuer="https://evil.example"), "invalid_token"),
        (lambda: session_claims("user_1", azp="https://evil.example"), "invalid_token"),
        (lambda: session_claims("user_1", nbf=int(time.time()) + 600), "invalid_token"),
        (lambda: session_claims("user_1", iat=int(time.time()) + 600), "invalid_token"),
        (lambda: session_claims("user_1", sub=""), "invalid_token"),
        (
            lambda: {k: v for k, v in session_claims("user_1").items() if k != "exp"},
            "invalid_token",
        ),
    ],
)
def test_bad_claims_are_rejected(make_claims: Callable[[], dict[str, object]], code: str) -> None:
    with pytest.raises(IdentityRejected) as raised:
        verify_rs256(mint(rsa_key(), make_claims()), _keys(), RULES)
    assert raised.value.code == code


def test_leeway_accepts_a_token_that_just_expired() -> None:
    token = mint(rsa_key(), session_claims("user_1", lifetime=-2))
    verify_rs256(token, _keys(), RULES)  # within the 5 s default leeway
    with pytest.raises(IdentityRejected):
        verify_rs256(token, _keys(), ClaimRules(issuer=ISSUER, leeway_seconds=0))


def test_azp_is_optional_but_checked_when_present() -> None:
    verify_rs256(mint(rsa_key(), session_claims("user_1", azp=None)), _keys(), RULES)
    # Origins compare without case or a trailing slash.
    verify_rs256(
        mint(rsa_key(), session_claims("user_1", azp=WEB_ORIGIN.upper() + "/")), _keys(), RULES
    )


def test_audience_is_enforced_when_configured() -> None:
    rules = ClaimRules(
        issuer=ISSUER, authorized_parties=RULES.authorized_parties, audience="runway"
    )
    verify_rs256(mint(rsa_key(), session_claims("user_1", aud=["runway", "x"])), _keys(), rules)
    with pytest.raises(IdentityRejected):
        verify_rs256(mint(rsa_key(), session_claims("user_1", aud="other")), _keys(), rules)


def test_forged_tampered_and_other_algorithm_tokens_are_rejected() -> None:
    forged = mint(rsa_key("attacker"), session_claims("user_1"))  # same kid, wrong key
    with pytest.raises(IdentityRejected, match="signature"):
        verify_rs256(forged, _keys(), RULES)

    header, _payload, signature = mint(rsa_key(), session_claims("user_1")).split(".")
    tampered_payload = b64url(json.dumps(session_claims("user_admin")).encode())
    with pytest.raises(IdentityRejected, match="signature"):
        verify_rs256(f"{header}.{tampered_payload}.{signature}", _keys(), RULES)

    for alg in ("none", "HS256", "RS512"):
        head = b64url(json.dumps({"alg": alg, "kid": "kid-1"}).encode())
        with pytest.raises(IdentityRejected, match="RS256"):
            verify_rs256(f"{head}.{tampered_payload}.{signature}", _keys(), RULES)

    for garbage in ("", "a.b", "a.b.c.d", "not a token", "x" * 20000):
        with pytest.raises(IdentityRejected):
            verify_rs256(garbage, _keys(), RULES)
    with pytest.raises(IdentityRejected, match="extensions"):
        verify_rs256(mint(rsa_key(), session_claims("u"), header={"crit": ["b64"]}), _keys(), RULES)


def test_unknown_kid_refreshes_at_most_once_per_interval() -> None:
    fetcher = FakeJwksFetcher(jwks((rsa_key(), "kid-1")))
    clock = [100.0]
    cache = _cache(fetcher, clock)
    stranger = mint(rsa_key(), session_claims("user_1"), kid="kid-unknown")
    for _ in range(20):
        with pytest.raises(IdentityRejected, match="unknown key"):
            verify_rs256(stranger, cache, RULES)
    assert fetcher.calls == 1
    clock[0] += 61
    with pytest.raises(IdentityRejected):
        verify_rs256(stranger, cache, RULES)
    assert fetcher.calls == 2


def test_rotated_keys_are_picked_up_and_cached_keys_survive_an_outage() -> None:
    fetcher = FakeJwksFetcher(jwks((rsa_key(), "kid-1")))
    clock = [0.0]
    cache = _cache(fetcher, clock)
    verify_rs256(mint(rsa_key(), session_claims("u")), cache, RULES)
    fetcher.document = jwks((rsa_key(), "kid-1"), (rsa_key("rotated"), "kid-2"))
    clock[0] += 61
    verify_rs256(mint(rsa_key("rotated"), session_claims("u"), kid="kid-2"), cache, RULES)
    assert fetcher.calls == 2
    # The provider is down after the TTL: cached keys keep working.
    fetcher.error = ConnectionError("down")
    clock[0] += 4000
    verify_rs256(mint(rsa_key(), session_claims("u")), cache, RULES)


def test_no_keys_at_all_is_unavailable_not_unauthorized() -> None:
    fetcher = FakeJwksFetcher()
    with pytest.raises(IdentityUnavailable):
        verify_rs256(mint(rsa_key(), session_claims("u")), _cache(fetcher), RULES)


def test_weak_or_wrong_jwks_entries_are_ignored() -> None:
    weak = jwks((rsa_key("weak", 1024), "kid-1"))["keys"][0]
    with pytest.raises(ValueError, match="too small"):
        rsa_public_key_from_jwk(weak)
    with pytest.raises(ValueError):
        rsa_public_key_from_jwk({**jwks((rsa_key(), "k"))["keys"][0], "use": "enc"})
    fetcher = FakeJwksFetcher({"keys": [weak, {"kty": "EC", "kid": "kid-1"}]})
    with pytest.raises(IdentityUnavailable):
        verify_rs256(mint(rsa_key("weak", 1024), session_claims("u")), _cache(fetcher), RULES)


def test_both_clerk_organization_claim_forms_are_read() -> None:
    assert organization_claims(session_claims("u", "org_a", form="v2", role="admin")) == (
        "org_a",
        "admin",
        "a",
    )
    assert organization_claims(session_claims("u", "org_b", form="v1", role="member")) == (
        "org_b",
        "member",
        "b",
    )
    both = {**session_claims("u", "org_v1", form="v1"), "o": {"id": "org_v2", "rol": "admin"}}
    assert organization_claims(both)[0] == "org_v2"
    assert organization_claims(session_claims("u")) == (None, None, None)


def _request(headers: dict[str, str]) -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


def _provider(fetcher: FakeJwksFetcher | None = None) -> ClerkIdentityProvider:
    config = ClerkConfig(
        issuer=ISSUER,
        jwks_url=f"{ISSUER}/.well-known/jwks.json",
        authorized_parties=frozenset({WEB_ORIGIN}),
    )
    return ClerkIdentityProvider(
        config, fetcher=fetcher or FakeJwksFetcher(jwks((rsa_key(), "kid-1")))
    )


def test_clerk_provider_reads_bearer_then_cookie() -> None:
    token = mint(rsa_key(), session_claims("user_1", "org_a", email="a@example.test", name="Ann"))
    identity = _provider().verify(_request({"authorization": f"Bearer {token}"}))
    assert (identity.user_id, identity.organization_id, identity.organization_role) == (
        "user_1",
        "org_a",
        "admin",
    )
    assert (identity.email, identity.display_name) == ("a@example.test", "Ann")
    from_cookie = _provider().verify(_request({"cookie": f"theme=dark; __session={token}"}))
    assert from_cookie.user_id == "user_1"
    with pytest.raises(IdentityRejected) as raised:
        _provider().verify(_request({}))
    assert raised.value.code == "authentication_required"


def test_a_pending_session_is_refused() -> None:
    token = mint(rsa_key(), session_claims("user_1", "org_a", sts="pending"))
    with pytest.raises(IdentityRejected) as raised:
        _provider().verify(_request({"authorization": f"Bearer {token}"}))
    assert raised.value.code == "session_pending"


def test_credential_precedence_and_api_key_detection() -> None:
    both = Headers({"authorization": "Bearer abc", "cookie": "__session=xyz"})
    assert presented_credential(both) == ("bearer", "abc")
    assert presented_credential(Headers({"cookie": "__session=xyz"})) == ("cookie", "xyz")
    assert presented_credential(Headers({"authorization": "Basic Zm9v"})) is None
    assert presents_api_key(Headers({"authorization": "Bearer rwy_live_x"}))
    assert not presents_api_key(Headers({"cookie": "__session=rwy_x"}))


# ------------------------------------------------------------------ svix


def test_svix_signature_round_trip_and_rejections() -> None:
    body = b'{"type":"user.created"}'
    now = 1_800_000_000
    headers = svix_headers(body, "msg_1", timestamp=now)
    assert svix.verify([WEBHOOK_SECRET], headers, body, now=now).delivery_id == "msg_1"
    # A rotated secret list accepts the delivery while either secret matches.
    other = "whsec_" + base64.b64encode(b"another-secret-of-32-bytes-long!").decode()
    assert svix.verify([other, WEBHOOK_SECRET], headers, body, now=now)
    # Several signatures in one header: any v1 match is enough.
    multi = {**headers, "svix-signature": "v1,AAAA " + headers["svix-signature"]}
    assert svix.verify([WEBHOOK_SECRET], multi, body, now=now)

    def refused(headers: dict[str, str], payload: bytes = body, at: int = now) -> None:
        with pytest.raises(svix.WebhookSignatureInvalid):
            svix.verify([WEBHOOK_SECRET], headers, payload, now=at)

    refused(headers, b'{"type":"user.deleted"}')
    refused({**headers, "svix-id": "msg_2"})
    refused(headers, at=now + 301)
    refused(headers, at=now - 301)
    refused({**headers, "svix-signature": headers["svix-signature"].replace("v1,", "v2,")})
    refused({k: v for k, v in headers.items() if k != "svix-signature"})
    refused({**headers, "svix-timestamp": "soon"})
    with pytest.raises(svix.WebhookSignatureInvalid):
        svix.verify([other], headers, body, now=now)


# ------------------------------------------------------------------ webhook payloads


def test_clerk_payloads_become_neutral_events() -> None:
    user = parse_clerk_event(
        {
            "type": "user.updated",
            "timestamp": 1_800_000_000_000,
            "data": {
                "id": "user_1",
                "primary_email_address_id": "idn_2",
                "email_addresses": [
                    {"id": "idn_1", "email_address": "old@example.test"},
                    {"id": "idn_2", "email_address": "primary@example.test"},
                ],
                "first_name": "Ann",
                "last_name": "Lee",
            },
        }
    )
    assert user.event == UserChanged("user_1", "primary@example.test", "Ann Lee")
    assert user.event_at is not None and user.event_at.year == 2027

    created = parse_clerk_event(membership_event("created", "org_a", "user_1", role="org:admin"))
    assert isinstance(created.event, MembershipChanged)
    assert created.event.provider_role == "admin"
    assert created.event.subject_key == "membership:org_a:user_1"
    removed = parse_clerk_event(membership_event("deleted", "org_a", "user_1"))
    assert removed.event == MembershipRemoved("org_a", "user_1")

    ignored = parse_clerk_event({"type": "session.created", "data": {}})
    assert ignored.event is None
    for bad in ([], {"data": {}}, {"type": "user.created"}, {"type": "user.created", "data": {}}):
        with pytest.raises(WebhookPayloadInvalid):
            parse_clerk_event(bad)


# ------------------------------------------------------------------ settings


def test_hosted_clerk_settings_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings(data_dir=tmp_path / "data", profile="hosted", identity_provider="clerk")
    message = str(raised.value)
    assert "RUNWAY_CLERK_ISSUER" in message
    assert "RUNWAY_CLERK_WEBHOOK_SECRET" in message
    assert "RUNWAY_WEB_ORIGINS" in message
    with pytest.raises(ValidationError, match="one of clerk"):
        Settings(data_dir=tmp_path / "data", profile="hosted", identity_provider="okta")
    with pytest.raises(ValidationError, match="https://host"):
        Settings(
            data_dir=tmp_path / "data",
            profile="hosted",
            identity_provider="clerk",
            web_origins="http://app.example.com,https://ok.example.com/path",
        )
    # Fully configured Clerk: only the SQLite refusal remains (Postgres support
    # is separate work).
    with pytest.raises(ValidationError) as raised:
        Settings(
            data_dir=tmp_path / "data",
            profile="hosted",
            identity_provider="clerk",
            clerk_issuer=ISSUER,
            clerk_webhook_secret=WEBHOOK_SECRET,
            web_origins=WEB_ORIGIN,
        )
    assert str(raised.value).count("requires") == 1
    assert "SQLite is local-only" in str(raised.value)
    assert "CLERK" not in str(raised.value) and "WEB_ORIGINS" not in str(raised.value)


def test_local_profile_ignores_hosted_settings_and_derives_defaults(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        web_base_url="https://App.Example.com/dashboard",
        clerk_issuer=ISSUER + "/",
        clerk_webhook_secret=f"{WEBHOOK_SECRET}, whsec_b3RoZXI=",
    )
    assert settings.profile == "local"
    assert settings.hosted_web_origin_list == ["https://app.example.com"]
    assert settings.clerk_jwks_endpoint == f"{ISSUER}/.well-known/jwks.json"
    assert settings.clerk_authorized_party_list == ["https://app.example.com"]
    assert settings.clerk_webhook_secrets == [WEBHOOK_SECRET, "whsec_b3RoZXI="]
    assert WEBHOOK_SECRET not in repr(settings)
    assert "clerk_webhook_secret" not in settings.public_dict()
    assert build_identity_provider(settings) is None  # no provider named


def test_registry_builds_clerk_without_touching_the_network(tmp_path: Path) -> None:
    fetcher = FakeJwksFetcher(jwks((rsa_key(), "kid-1")))
    settings = Settings(data_dir=tmp_path / "data", identity_provider="clerk", clerk_issuer=ISSUER)
    provider = build_identity_provider(settings, fetcher=fetcher)
    assert provider is not None and provider.name == "clerk"
    assert fetcher.calls == 0
    with pytest.raises(ValueError, match="unknown identity provider"):
        build_identity_provider(settings.model_copy(update={"identity_provider": "okta"}))
