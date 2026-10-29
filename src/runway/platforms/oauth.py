from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

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
        """Validate and consume a state value; each state may be used exactly once.

        The check-then-set is a single conditional UPDATE, so two concurrent
        callbacks cannot both pass the unconsumed/unexpired gate: only the
        transaction whose statement matches all conditions reports rowcount 1.
        """
        now = utcnow()
        with self.database.session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(OAuthState)
                    .where(
                        OAuthState.state == state,
                        OAuthState.platform == platform.value,
                        OAuthState.consumed_at.is_(None),
                        OAuthState.expires_at > now,
                    )
                    .values(consumed_at=now)
                ),
            )
            record = session.scalar(select(OAuthState).where(OAuthState.state == state))
            if result.rowcount != 1:
                if (
                    record is None
                    or record.platform != platform.value
                    or record.consumed_at is not None
                ):
                    raise OAuthStateError("invalid_state")
                raise OAuthStateError("expired_state")
            assert record is not None
            session.expunge(record)
            return record
