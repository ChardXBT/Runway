"""Per-workspace quotas and the publishing kill switches (spec 05 section 6).

Other code calls these functions; it never reads `workspace_limits` itself.

* `quotas_for(session, settings, workspace_id)` -> `WorkspaceQuotas`
* `enforce_quota(quotas, "connected_accounts", used=n)` raises 403 `quota_exceeded`
* `publishing_allowed(session, settings, workspace_id)` -> bool, checked by the
  worker before every send; `publishing_gate(...)` also says why not.

In `local` every quota is unlimited (invariant 17); the kill switches apply in
both profiles because they can only stop publishing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields, replace
from typing import Literal

from sqlalchemy.orm import Session

from runway.auth.problems import Problem
from runway.config import Settings
from runway.db.hosted_models import WorkspaceLimits

logger = logging.getLogger(__name__)

QuotaName = Literal[
    "storage_bytes", "connected_accounts", "posts_per_day", "api_requests_per_minute"
]
QUOTA_NAMES: tuple[QuotaName, ...] = (
    "storage_bytes",
    "connected_accounts",
    "posts_per_day",
    "api_requests_per_minute",
)
# Spec 05 section 7: alert when a workspace passes 80% of a quota.
NEAR_LIMIT_RATIO = 0.8


@dataclass(frozen=True, slots=True)
class WorkspaceQuotas:
    """Effective limits for one workspace. None means unlimited."""

    storage_bytes: int | None
    connected_accounts: int | None
    posts_per_day: int | None
    api_requests_per_minute: int | None

    def limit(self, name: QuotaName) -> int | None:
        value: int | None = getattr(self, name)
        return value


UNLIMITED = WorkspaceQuotas(None, None, None, None)


@dataclass(frozen=True, slots=True)
class PublishingGate:
    allowed: bool
    reason: str | None = None


def default_quotas(settings: Settings) -> WorkspaceQuotas:
    if settings.profile != "hosted":
        return UNLIMITED
    return WorkspaceQuotas(
        storage_bytes=settings.quota_storage_bytes,
        connected_accounts=settings.quota_connected_accounts,
        posts_per_day=settings.quota_posts_per_day,
        api_requests_per_minute=settings.quota_api_requests_per_minute,
    )


def quotas_for(session: Session, settings: Settings, workspace_id: int) -> WorkspaceQuotas:
    """Configured defaults overlaid with the workspace's operator overrides."""
    quotas = default_quotas(settings)
    if settings.profile != "hosted":
        return quotas
    limits = session.get(WorkspaceLimits, workspace_id)
    if limits is None:
        return quotas
    overrides = {
        item.name: getattr(limits, item.name)
        for item in fields(WorkspaceQuotas)
        if getattr(limits, item.name) is not None
    }
    return replace(quotas, **overrides)


def enforce_quota(
    quotas: WorkspaceQuotas,
    name: QuotaName,
    *,
    used: int,
    adding: int = 1,
    workspace_id: int | None = None,
) -> None:
    """Refuse an action that would take `name` past its limit (403 `quota_exceeded`)."""
    limit = quotas.limit(name)
    if limit is None:
        return
    if used + adding > limit:
        raise Problem(
            403,
            "quota_exceeded",
            "Workspace quota reached",
            f"this workspace may use at most {limit} {name.replace('_', ' ')}",
        )
    if used + adding >= NEAR_LIMIT_RATIO * limit:
        logger.warning(
            "workspace quota above %d%%: %s",
            int(NEAR_LIMIT_RATIO * 100),
            name,
            extra={"event_type": "quota_near_limit", "quota": name, "workspace_id": workspace_id},
        )


def set_quota_overrides(
    session: Session,
    workspace_id: int,
    *,
    updated_by: str,
    clear: bool = False,
    **values: int | None,
) -> WorkspaceLimits:
    """Set (or with `clear=True`, reset to defaults) a workspace's quota overrides."""
    unknown = set(values) - set(QUOTA_NAMES)
    if unknown:
        raise ValueError(f"unknown quota {sorted(unknown)!r}")
    limits = _limits_row(session, workspace_id)
    for name in QUOTA_NAMES:
        if clear:
            setattr(limits, name, None)
        elif name in values and values[name] is not None:
            value = values[name]
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
            setattr(limits, name, value)
    limits.updated_by = updated_by
    session.flush()
    return limits


def publishing_gate(session: Session, settings: Settings, workspace_id: int) -> PublishingGate:
    if settings.publishing_paused:
        return PublishingGate(False, "publishing is paused for every workspace by the operator")
    limits = session.get(WorkspaceLimits, workspace_id)
    if limits is not None and limits.publishing_disabled:
        reason = limits.publishing_disabled_reason or "no reason recorded"
        return PublishingGate(False, f"publishing is disabled for this workspace: {reason}")
    return PublishingGate(True)


def publishing_allowed(session: Session, settings: Settings, workspace_id: int) -> bool:
    """The worker's check before every send: global pause and the workspace kill switch."""
    return publishing_gate(session, settings, workspace_id).allowed


def set_publishing_disabled(
    session: Session,
    workspace_id: int,
    *,
    disabled: bool,
    reason: str | None,
    updated_by: str,
) -> WorkspaceLimits:
    """Operator kill switch for one workspace."""
    limits = _limits_row(session, workspace_id)
    limits.publishing_disabled = disabled
    limits.publishing_disabled_reason = reason if disabled else None
    limits.updated_by = updated_by
    session.flush()
    return limits


def _limits_row(session: Session, workspace_id: int) -> WorkspaceLimits:
    limits = session.get(WorkspaceLimits, workspace_id)
    if limits is None:
        limits = WorkspaceLimits(workspace_id=workspace_id, publishing_disabled=False)
        session.add(limits)
    return limits
