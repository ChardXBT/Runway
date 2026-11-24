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
        payload: str | None = None,
        purpose: str = "connect",
        account_id: int | None = None,
        workspace_id: int = 1,
    ) -> IssuedState:
        """A single-use state row for one OAuth flow.

        `purpose` records why the flow was started ("connect", "reconnect" or
        "enable_publishing"); `account_id` binds it to the connected account it
        was issued for, so the callback can refuse a token for a different
        channel (spec 08 section 1: identity check before every write).

        The states table has no payload column, so a non-PKCE state carries
        its `payload` (e.g. the scope list of an incremental-consent request)
        in `code_verifier`, which is empty there anyway. `read_state_storage`
        splits the column back into verifier and payload.
        """
        verifier = pkce_verifier() if use_pkce else None
        stored = verifier if verifier is not None else payload
        state = secrets.token_urlsafe(32)
        now = utcnow()
        with self.database.session() as session:
            session.add(
                OAuthState(
                    platform=platform.value,
                    state=state,
                    code_verifier=stored,
                    redirect_uri=redirect_uri,
                    purpose=purpose,
                    account_id=account_id,
                    workspace_id=workspace_id,
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

    def peek(self, platform: Platform, *, state: str) -> OAuthState | None:
        """Read a live, unconsumed state without consuming it.

        Used by two-step flows (Unified Login's account pick) that list the
        stored payload first and consume the state only on the final POST.
        """
        now = utcnow()
        with self.database.session() as session:
            record = session.scalar(
                select(OAuthState).where(
                    OAuthState.state == state,
                    OAuthState.platform == platform.value,
                    OAuthState.consumed_at.is_(None),
                    OAuthState.expires_at > now,
                )
            )
            if record is not None:
                session.expunge(record)
            return record


def read_state_storage(record: OAuthState) -> tuple[str | None, str | None]:
    """Split a consumed state's stored text into (code_verifier, payload).

    PKCE verifiers are base64url and never start with `{`; a `{`-prefixed
    value is the JSON payload a non-PKCE flow stored in that column.
    """
    text = record.code_verifier
    if text is not None and text.lstrip().startswith("{"):
        return None, text
    return text, None
