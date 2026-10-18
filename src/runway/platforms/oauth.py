from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from runway.db.base import Database
from runway.db.models import OAuthState, utcnow
from runway.platforms.capabilities import Platform
from runway.platforms.tokens import pkce_verifier

STATE_TTL = timedelta(minutes=10)


class OAuthStateError(Exception):
    pass


@dataclass(frozen=True)
class IssuedState:
    state: str
    code_verifier: str | None


class OAuthStateService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def issue(
        self,
        platform: Platform,
        *,
        redirect_uri: str,
        use_pkce: bool,
    ) -> IssuedState:
        verifier = pkce_verifier() if use_pkce else None
        state = secrets.token_urlsafe(32)
        now = utcnow()
        with self.database.session() as session:
            session.add(
                OAuthState(
                    platform=platform.value,
                    state=state,
                    code_verifier=verifier,
                    redirect_uri=redirect_uri,
                    created_at=now,
                    expires_at=now + STATE_TTL,
                )
            )
        return IssuedState(state=state, code_verifier=verifier)

    def consume(self, platform: Platform, *, state: str) -> OAuthState:
        """Validate and consume a state value; each state may be used exactly once."""
        with self.database.session() as session:
            record = session.scalar(select(OAuthState).where(OAuthState.state == state))
            if record is None or record.platform != platform.value:
                raise OAuthStateError("invalid_state")
            if record.consumed_at is not None:
                raise OAuthStateError("invalid_state")
            expires_at = record.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                raise OAuthStateError("expired_state")
            record.consumed_at = utcnow()
            session.flush()
            session.expunge(record)
            return record
