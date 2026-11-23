"""Workspace release policy and per-key release grants (spec 06 section 4).

`agent_release`:

- `never` (default): no key may hold `posts:release`.
- `per_key`: an owner may grant `posts:release` to a named key, per platform,
  each with a daily cap.

TikTok targets can never be released by a key: the platform requires the
person to choose privacy and interactions by hand and to consent
(research/tiktok section 7). `authorize_release` is the single check; the
publishing engine's release route, the request-policy guard and the MCP
`release_target` tool all call it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.auth.audit import record_audit
from runway.auth.key_limits import releases_today
from runway.auth.principal import POSTS_RELEASE, Principal, require
from runway.auth.problems import Problem, ProblemCode
from runway.db.models import ApiKeyReleaseGrant, WorkspacePolicy, utcnow

AgentRelease = Literal["never", "per_key"]
AGENT_RELEASE_VALUES: tuple[str, ...] = get_args(AgentRelease)
DEFAULT_AGENT_RELEASE: AgentRelease = "never"
# The platform's own rules require a person; no policy can change this.
HUMAN_ONLY_PLATFORMS = frozenset({"tiktok"})
RELEASABLE_PLATFORMS = frozenset({"youtube", "instagram"})


class ReleaseGrantInput(BaseModel):
    platform: Literal["youtube", "instagram", "tiktok"]
    daily_cap: int | None = Field(default=None, ge=1, le=1000)


class ReleaseGrantView(BaseModel):
    platform: str
    daily_cap: int


@dataclass(frozen=True, slots=True)
class PolicyView:
    agent_release: AgentRelease
    updated_by: str | None
    updated_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        return {
            "agent_release": self.agent_release,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "human_only_platforms": sorted(HUMAN_ONLY_PLATFORMS),
        }


def _as_policy(value: str | None) -> AgentRelease:
    return "per_key" if value == "per_key" else "never"


def agent_release_policy(session: Session, workspace_id: int) -> AgentRelease:
    row = session.get(WorkspacePolicy, workspace_id)
    return _as_policy(row.agent_release if row is not None else None)


def policy_view(session: Session, workspace_id: int) -> PolicyView:
    row = session.get(WorkspacePolicy, workspace_id)
    if row is None:
        return PolicyView(DEFAULT_AGENT_RELEASE, None, None)
    return PolicyView(_as_policy(row.agent_release), row.updated_by, row.updated_at)


def set_agent_release_policy(
    session: Session, actor: Principal, value: AgentRelease
) -> PolicyView:
    """Change the workspace policy. Turning it to `never` disables every grant at once."""
    if value not in AGENT_RELEASE_VALUES:
        raise Problem.of(ProblemCode.VALIDATION_FAILED, "agent_release must be never or per_key")
    row = session.get(WorkspacePolicy, actor.workspace_id)
    before = _as_policy(row.agent_release if row is not None else None)
    if row is None:
        row = WorkspacePolicy(workspace_id=actor.workspace_id)
        session.add(row)
    row.agent_release = value
    row.updated_by = f"{actor.kind}:{actor.id}"
    row.updated_at = utcnow()
    session.flush()
    record_audit(
        session,
        actor,
        "policy.agent_release",
        target_type="workspace",
        target_id=actor.workspace_id,
        detail={"before": before, "after": value},
    )
    return policy_view(session, actor.workspace_id)


def release_grants(session: Session, key_id: int) -> list[ReleaseGrantView]:
    rows = session.scalars(
        select(ApiKeyReleaseGrant)
        .where(ApiKeyReleaseGrant.key_id == key_id)
        .order_by(ApiKeyReleaseGrant.platform)
    ).all()
    return [ReleaseGrantView(platform=row.platform, daily_cap=row.daily_cap) for row in rows]


def validate_grants(
    grants: Iterable[ReleaseGrantInput], *, default_cap: int
) -> list[ReleaseGrantView]:
    """Reject TikTok and duplicates; fill in the default daily cap."""
    seen: dict[str, ReleaseGrantView] = {}
    for grant in grants:
        if grant.platform in HUMAN_ONLY_PLATFORMS:
            raise Problem.of(
                ProblemCode.HUMAN_REQUIRED,
                "TikTok posts must be released by a person; no key can be granted that",
            )
        if grant.platform in seen:
            raise Problem.of(ProblemCode.VALIDATION_FAILED, "each platform may be granted once")
        seen[grant.platform] = ReleaseGrantView(
            platform=grant.platform, daily_cap=grant.daily_cap or default_cap
        )
    return [seen[platform] for platform in sorted(seen)]


def replace_grants(
    session: Session,
    *,
    workspace_id: int,
    key_id: int,
    grants: Sequence[ReleaseGrantView],
    created_by: str,
) -> None:
    for row in session.scalars(
        select(ApiKeyReleaseGrant).where(ApiKeyReleaseGrant.key_id == key_id)
    ).all():
        session.delete(row)
    session.flush()
    for grant in grants:
        session.add(
            ApiKeyReleaseGrant(
                workspace_id=workspace_id,
                key_id=key_id,
                platform=grant.platform,
                daily_cap=grant.daily_cap,
                created_by=created_by,
                created_at=utcnow(),
            )
        )
    session.flush()


def key_may_release(session: Session, workspace_id: int, key_id: int) -> bool:
    """Whether a key's stored `posts:release` is effective right now."""
    if agent_release_policy(session, workspace_id) != "per_key":
        return False
    return (
        session.scalar(
            select(ApiKeyReleaseGrant.id).where(ApiKeyReleaseGrant.key_id == key_id).limit(1)
        )
        is not None
    )


def human_required() -> Problem:
    return Problem.of(
        ProblemCode.HUMAN_REQUIRED,
        "TikTok requires the creator to choose privacy and interactions and to consent "
        "in Runway; an API key can never release a TikTok post",
    )


def authorize_release(
    session: Session, principal: Principal, *, platform: str | None, now: datetime | None = None
) -> None:
    """Raise unless `principal` may release a target on `platform` now.

    Order: a key releasing TikTok is always 403 `human_required`; then the
    `posts:release` scope (403 `scope_missing`); then, for keys, the workspace
    policy, the per-platform grant and its daily cap (429 `rate_limited`).
    `platform=None` (target unknown) checks only what does not depend on it.
    """
    if principal.kind == "agent" and platform in HUMAN_ONLY_PLATFORMS:
        raise human_required()
    require(principal, POSTS_RELEASE)
    if principal.kind != "agent":
        return
    if agent_release_policy(session, principal.workspace_id) != "per_key":
        raise Problem.of(
            ProblemCode.SCOPE_MISSING,
            "the workspace policy agent_release is never; no key may release",
        )
    if platform is None:
        return
    key_id = int(principal.id)
    grant = session.scalar(
        select(ApiKeyReleaseGrant).where(
            ApiKeyReleaseGrant.key_id == key_id, ApiKeyReleaseGrant.platform == platform
        )
    )
    if grant is None:
        raise Problem.of(
            ProblemCode.SCOPE_MISSING,
            f"this key holds no release grant for {platform}",
        )
    if releases_today(session, principal, platform, now) >= grant.daily_cap:
        raise Problem.of(
            ProblemCode.RATE_LIMITED,
            f"this key reached its daily cap of {grant.daily_cap} {platform} releases",
            extra={"daily_cap": grant.daily_cap},
        )
