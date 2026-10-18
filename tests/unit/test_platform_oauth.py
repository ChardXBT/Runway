from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from runway.db.base import Database
from runway.db.models import OAuthState
from runway.platforms.capabilities import Platform
from runway.platforms.oauth import OAuthStateError, OAuthStateService
from runway.platforms.tokens import pkce_challenge


def test_state_is_single_use(database: Database) -> None:
    service = OAuthStateService(database)
    issued = service.issue(Platform.youtube, redirect_uri="http://x/cb", use_pkce=True)
    record = service.consume(Platform.youtube, state=issued.state)
    assert record.code_verifier == issued.code_verifier
    with pytest.raises(OAuthStateError):
        service.consume(Platform.youtube, state=issued.state)


def test_expired_state_is_rejected(database: Database) -> None:
    service = OAuthStateService(database)
    issued = service.issue(Platform.youtube, redirect_uri="http://x/cb", use_pkce=True)
    with database.session() as session:
        record = session.query(OAuthState).filter_by(state=issued.state).one()
        record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    with pytest.raises(OAuthStateError, match="expired_state"):
        service.consume(Platform.youtube, state=issued.state)


def test_platform_mismatch_is_rejected(database: Database) -> None:
    service = OAuthStateService(database)
    issued = service.issue(Platform.youtube, redirect_uri="http://x/cb", use_pkce=True)
    with pytest.raises(OAuthStateError, match="invalid_state"):
        service.consume(Platform.instagram, state=issued.state)


def test_pkce_verifier_stored_for_youtube_only(database: Database) -> None:
    service = OAuthStateService(database)
    youtube_issued = service.issue(Platform.youtube, redirect_uri="http://x/cb", use_pkce=True)
    instagram_issued = service.issue(Platform.instagram, redirect_uri="http://x/cb", use_pkce=False)
    assert youtube_issued.code_verifier is not None
    assert instagram_issued.code_verifier is None
    consumed = service.consume(Platform.youtube, state=youtube_issued.state)
    assert consumed.code_verifier == youtube_issued.code_verifier
    challenge = pkce_challenge(consumed.code_verifier or "")
    assert len(challenge) == 43


def test_unknown_state_is_rejected(database: Database) -> None:
    service = OAuthStateService(database)
    with pytest.raises(OAuthStateError, match="invalid_state"):
        service.consume(Platform.youtube, state="never-issued")
