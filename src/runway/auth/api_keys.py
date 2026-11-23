"""API keys for agents and scripts (spec 06 section 3).

Format: ``rwy_<env>_<8-char prefix>_<43-char secret>``. Only the prefix and a
SHA-256 hash of the whole key are stored; the key is shown once, at creation.
``<env>`` is ``local`` or ``live`` (hosted), so a key from one installation
kind is refused by the other.

`resolve_api_key` is the one function `runway.auth.principal.resolve_principal`
calls when a request carries ``Authorization: Bearer rwy_...``, in both
profiles.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.api.v1.pagination import CursorPage, CursorParams, paginate
from runway.auth.audit import record_audit
from runway.auth.key_limits import SlidingWindowLimiter, rate_limited
from runway.auth.principal import (
    ACCOUNTS_MANAGE,
    ACCOUNTS_READ,
    ALL_SCOPES,
    API_KEY_PREFIX,
    CONTENT_READ,
    INBOX_DRAFT,
    INBOX_READ,
    INBOX_REPLY,
    MEDIA_WRITE,
    POSTS_READ,
    POSTS_RELEASE,
    POSTS_SCHEDULE,
    POSTS_WRITE,
    WORKSPACE_ADMIN,
    Principal,
    require,
)
from runway.auth.problems import Problem, ProblemCode
from runway.auth.release_policy import (
    ReleaseGrantInput,
    ReleaseGrantView,
    agent_release_policy,
    key_may_release,
    release_grants,
    replace_grants,
    validate_grants,
)
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ApiKey, Workspace, utcnow

# Spec 06 section 3: what an "agent" key gets unless the creator says otherwise.
# Deliberately without posts:release, inbox:reply and accounts:manage.
DEFAULT_AGENT_SCOPES: frozenset[str] = frozenset(
    {
        ACCOUNTS_READ,
        CONTENT_READ,
        INBOX_READ,
        INBOX_DRAFT,
        MEDIA_WRITE,
        POSTS_READ,
        POSTS_WRITE,
        POSTS_SCHEDULE,
    }
)
NEVER_DEFAULT_SCOPES = frozenset({POSTS_RELEASE, INBOX_REPLY, ACCOUNTS_MANAGE})

KeyEnv = Literal["local", "live"]
_ENV_FOR_PROFILE: dict[str, KeyEnv] = {"local": "local", "hosted": "live"}
_KEY_PATTERN = re.compile(r"^rwy_(local|live)_([0-9a-f]{8})_([A-Za-z0-9_-]{32,128})$")
_SECRET_BYTES = 32
# last_used_at is a hint, not a ledger: write it at most once a minute per key.
LAST_USED_RESOLUTION = timedelta(minutes=1)
_PRINCIPAL_STATE = "runway_api_key_principal"
_LIMITER_STATE = "runway_api_key_limiter"


def key_env(settings: Settings) -> KeyEnv:
    return _ENV_FOR_PROFILE.get(settings.profile, "local")


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    secret: str
    prefix: str
    key_hash: str


@dataclass(frozen=True, slots=True)
class ParsedKey:
    env: str
    prefix: str


def generate_key(env: KeyEnv) -> GeneratedKey:
    prefix = secrets.token_hex(4)
    secret = f"{API_KEY_PREFIX}{env}_{prefix}_{secrets.token_urlsafe(_SECRET_BYTES)}"
    return GeneratedKey(secret=secret, prefix=prefix, key_hash=hash_key(secret))


def parse_key(raw: str) -> ParsedKey | None:
    match = _KEY_PATTERN.match(raw)
    if match is None:
        return None
    return ParsedKey(env=match.group(1), prefix=match.group(2))


def bearer_credential(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, credential = authorization.partition(" ")
    if scheme.casefold() != "bearer":
        return None
    return credential.strip() or None


KeyState = Literal["active", "expired", "revoked"]


class ApiKeyView(BaseModel):
    """A key as the owner sees it later. Never contains the secret or its hash."""

    id: int
    name: str
    prefix: str
    scopes: list[str]
    effective_scopes: list[str]
    state: KeyState
    created_by: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    release_grants: list[ReleaseGrantView]


class CreatedApiKey(BaseModel):
    key: ApiKeyView
    # Shown once. Runway keeps only its SHA-256 hash.
    secret: str


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _stored_scopes(row: ApiKey) -> frozenset[str]:
    try:
        raw: Any = json.loads(row.scopes_json or "[]")
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(scope for scope in raw if isinstance(scope, str) and scope in ALL_SCOPES)


def invalid_key(detail: str = "the API key is not valid") -> Problem:
    return Problem.of(
        ProblemCode.AUTHENTICATION_REQUIRED,
        detail,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


class ApiKeyService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        clock: Any = utcnow,
    ) -> None:
        self.database = database
        self.settings = settings
        self.clock = clock

    # ------------------------------------------------------------------ views

    def _state(self, row: ApiKey) -> KeyState:
        if row.revoked_at is not None:
            return "revoked"
        expires = _aware(row.expires_at)
        if expires is not None and expires <= self.clock():
            return "expired"
        return "active"

    def _effective(self, session: Session, row: ApiKey) -> frozenset[str]:
        scopes = _stored_scopes(row)
        if POSTS_RELEASE in scopes and not key_may_release(session, row.workspace_id, row.id):
            scopes = scopes - {POSTS_RELEASE}
        return scopes

    def view(self, session: Session, row: ApiKey) -> ApiKeyView:
        state = self._state(row)
        return ApiKeyView(
            id=row.id,
            name=row.name,
            prefix=row.prefix,
            scopes=sorted(_stored_scopes(row)),
            effective_scopes=sorted(self._effective(session, row)) if state == "active" else [],
            state=state,
            created_by=row.created_by,
            created_at=_aware(row.created_at) or self.clock(),
            last_used_at=_aware(row.last_used_at),
            expires_at=_aware(row.expires_at),
            revoked_at=_aware(row.revoked_at),
            release_grants=release_grants(session, row.id),
        )

    def _row(self, session: Session, workspace_id: int, key_id: int) -> ApiKey:
        row = session.get(ApiKey, key_id)
        if row is None or row.workspace_id != workspace_id:
            raise Problem.of(ProblemCode.NOT_FOUND, "no such API key in this workspace")
        return row

    # --------------------------------------------------------------- mutation

    def _check_scopes(
        self, session: Session, creator: Principal, scopes: frozenset[str], grants: bool
    ) -> None:
        unknown = scopes - ALL_SCOPES
        if unknown:
            raise Problem.of(
                ProblemCode.VALIDATION_FAILED, f"unknown scopes: {', '.join(sorted(unknown))}"
            )
        lacking = scopes - creator.scopes
        if lacking:
            # Spec 06 section 3: a key never receives a scope its creator lacks.
            raise Problem.of(
                ProblemCode.SCOPE_MISSING,
                f"you cannot grant scopes you do not hold: {', '.join(sorted(lacking))}",
            )
        if POSTS_RELEASE in scopes:
            if agent_release_policy(session, creator.workspace_id) != "per_key":
                raise Problem.of(
                    ProblemCode.VALIDATION_FAILED,
                    "the workspace policy agent_release is never; no key may hold posts:release",
                )
            if not grants:
                raise Problem.of(
                    ProblemCode.VALIDATION_FAILED,
                    "posts:release needs at least one per-platform release grant",
                )
        elif grants:
            raise Problem.of(
                ProblemCode.VALIDATION_FAILED, "release grants need the posts:release scope"
            )

    def create(
        self,
        creator: Principal,
        *,
        name: str,
        scopes: Iterable[str] | None = None,
        expires_at: datetime | None = None,
        release_grants: Sequence[ReleaseGrantInput] = (),
    ) -> CreatedApiKey:
        """Create a key; the returned `secret` is the only copy that will ever exist."""
        require(creator, WORKSPACE_ADMIN)
        clean_name = name.strip()
        if not clean_name:
            raise Problem.of(ProblemCode.VALIDATION_FAILED, "a key needs a name")
        chosen = frozenset(scopes) if scopes is not None else DEFAULT_AGENT_SCOPES
        if not chosen:
            raise Problem.of(ProblemCode.VALIDATION_FAILED, "a key needs at least one scope")
        expires = _aware(expires_at)
        if expires is not None and expires <= self.clock():
            raise Problem.of(ProblemCode.VALIDATION_FAILED, "expires_at must be in the future")
        grants = validate_grants(
            release_grants, default_cap=self.settings.api_key_default_release_cap
        )
        generated = generate_key(key_env(self.settings))
        with self.database.session() as session:
            self._check_scopes(session, creator, chosen, bool(grants))
            row = ApiKey(
                workspace_id=creator.workspace_id,
                name=clean_name[:200],
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                scopes_json=json.dumps(sorted(chosen)),
                created_by=f"{creator.kind}:{creator.id}",
                created_at=self.clock(),
                expires_at=expires,
            )
            session.add(row)
            session.flush()
            replace_grants(
                session,
                workspace_id=creator.workspace_id,
                key_id=row.id,
                grants=grants,
                created_by=f"{creator.kind}:{creator.id}",
            )
            record_audit(
                session,
                creator,
                "api_key.create",
                target_type="api_key",
                target_id=row.id,
                detail={
                    "name": row.name,
                    "prefix": row.prefix,
                    "scopes": sorted(chosen),
                    "release_platforms": [grant.platform for grant in grants],
                },
            )
            return CreatedApiKey(key=self.view(session, row), secret=generated.secret)

    def revoke(self, actor: Principal, key_id: int) -> ApiKeyView:
        require(actor, WORKSPACE_ADMIN)
        with self.database.session() as session:
            row = self._row(session, actor.workspace_id, key_id)
            if row.revoked_at is None:
                row.revoked_at = self.clock()
                session.flush()
                record_audit(
                    session,
                    actor,
                    "api_key.revoke",
                    target_type="api_key",
                    target_id=row.id,
                    detail={"name": row.name, "prefix": row.prefix},
                )
            return self.view(session, row)

    def set_release_grants(
        self, actor: Principal, key_id: int, grants: Sequence[ReleaseGrantInput]
    ) -> ApiKeyView:
        """Replace a key's release grants. An empty list takes `posts:release` away."""
        require(actor, WORKSPACE_ADMIN)
        cleaned = validate_grants(grants, default_cap=self.settings.api_key_default_release_cap)
        with self.database.session() as session:
            row = self._row(session, actor.workspace_id, key_id)
            if row.revoked_at is not None:
                raise Problem.of(ProblemCode.VALIDATION_FAILED, "this key is revoked")
            scopes = set(_stored_scopes(row))
            if cleaned:
                require(actor, POSTS_RELEASE)
                if agent_release_policy(session, actor.workspace_id) != "per_key":
                    raise Problem.of(
                        ProblemCode.VALIDATION_FAILED,
                        "set the workspace policy agent_release to per_key first",
                    )
                scopes.add(POSTS_RELEASE)
            else:
                scopes.discard(POSTS_RELEASE)
            row.scopes_json = json.dumps(sorted(scopes))
            replace_grants(
                session,
                workspace_id=actor.workspace_id,
                key_id=row.id,
                grants=cleaned,
                created_by=f"{actor.kind}:{actor.id}",
            )
            record_audit(
                session,
                actor,
                "api_key.release_grants",
                target_type="api_key",
                target_id=row.id,
                detail={"grants": [grant.model_dump() for grant in cleaned]},
            )
            return self.view(session, row)

    # ------------------------------------------------------------------ reads

    def list(self, workspace_id: int, params: CursorParams) -> CursorPage[ApiKeyView]:
        with self.database.session() as session:
            rows, next_cursor = paginate(
                session,
                select(ApiKey).where(ApiKey.workspace_id == workspace_id),
                ApiKey.id,
                params,
                scope="api_keys",
            )
            return CursorPage[ApiKeyView](
                items=[self.view(session, row) for row in rows], next_cursor=next_cursor
            )

    def get(self, workspace_id: int, key_id: int) -> ApiKeyView:
        with self.database.session() as session:
            return self.view(session, self._row(session, workspace_id, key_id))

    # --------------------------------------------------------- authentication

    def authenticate(self, raw: str) -> Principal:
        """The agent principal for a presented key, or 401 `authentication_required`."""
        parsed = parse_key(raw)
        if parsed is None:
            raise invalid_key()
        if parsed.env != key_env(self.settings):
            raise invalid_key("this API key belongs to a different kind of Runway installation")
        presented = hash_key(raw)
        with self.database.session() as session:
            row = session.scalar(select(ApiKey).where(ApiKey.prefix == parsed.prefix))
            if row is None or not hmac.compare_digest(row.key_hash, presented):
                raise invalid_key()
            state = self._state(row)
            if state != "active":
                raise invalid_key(f"this API key is {state}")
            workspace = session.get(Workspace, row.workspace_id)
            if workspace is None or workspace.deleted_at is not None:
                raise invalid_key("this API key's workspace no longer exists")
            now = self.clock()
            last_used = _aware(row.last_used_at)
            if last_used is None or now - last_used >= LAST_USED_RESOLUTION:
                row.last_used_at = now
            return Principal(
                kind="agent",
                id=str(row.id),
                workspace_id=row.workspace_id,
                role=None,
                scopes=self._effective(session, row),
                display=row.name,
            )


def _limiter(request: Request, settings: Settings) -> SlidingWindowLimiter:
    state = request.app.state
    limiter = getattr(state, _LIMITER_STATE, None)
    if (
        not isinstance(limiter, SlidingWindowLimiter)
        or limiter.limit != settings.api_key_rate_limit_per_minute
    ):
        limiter = SlidingWindowLimiter(settings.api_key_rate_limit_per_minute)
        setattr(state, _LIMITER_STATE, limiter)
    return limiter


def resolve_api_key(settings: Settings, request: Request) -> Principal:
    """Resolve ``Authorization: Bearer rwy_...`` to an agent principal (both profiles).

    Never falls back to the local owner: an unknown, revoked or expired key is
    401, an app that cannot reach its database cannot verify keys and refuses.
    Resolved once per request (the idempotency wrapper and the route both ask)
    and counted once against the key's rate limit.
    """
    cached = getattr(request.state, _PRINCIPAL_STATE, None)
    if isinstance(cached, Principal):
        return cached
    raw = bearer_credential(request) or ""
    database = getattr(request.app.state, "database", None)
    if not isinstance(database, Database):
        raise Problem.of(
            ProblemCode.API_KEY_UNSUPPORTED,
            "this server cannot verify API keys; send the request without one",
        )
    principal = ApiKeyService(database, settings).authenticate(raw)
    retry_after = _limiter(request, settings).hit(f"{principal.workspace_id}:{principal.id}")
    if retry_after is not None:
        raise rate_limited(retry_after)
    setattr(request.state, _PRINCIPAL_STATE, principal)
    return principal
