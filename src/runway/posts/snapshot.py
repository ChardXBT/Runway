"""Everything known about one target at one moment: payload, hash, publisher, issues.

The API views, release and the worker all read targets through `snapshot`,
so the hash a person approves is computed exactly as the worker recomputes
it before sending.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.models import (
    AccountPublisher,
    ConnectedAccount,
    ContentDraft,
    DraftMedia,
    DraftTarget,
    MediaAssetV2,
)
from runway.mediastore import MediaStore
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.post_settings import (
    InvalidTargetSettings,
    TargetSettings,
    YouTubeVideoSettings,
    explicit_privacy,
    parse_settings,
    settings_issues,
)
from runway.platforms.publish_capability import (
    ASSISTED,
    AccountFacts,
    AppStatus,
    PublishCapability,
    publishing_capability,
)
from runway.platforms.publisher import Issue, has_errors
from runway.platforms.publisher_registry import PublisherRegistry
from runway.posts.payload import canonical_payload, payload_hash
from runway.posts.types import TargetPostType, platform_of

# Spec 03 section 6: native YouTube scheduling needs the upload to finish first.
NATIVE_MIN_LEAD = timedelta(minutes=15)
NATIVE_WARN_LEAD = timedelta(hours=1)
NATIVE_LARGE_FILE_BYTES = 1024**3
# Spec 03 section 6 missed-window rule: a target less than this late is still
# sent; later than that a person must choose a new time.
MISSED_WINDOW = timedelta(minutes=60)

# Media items each post type takes: (minimum, maximum, kinds allowed).
MEDIA_RULES: dict[TargetPostType, tuple[int, int, frozenset[str]]] = {
    TargetPostType.YOUTUBE_VIDEO: (1, 1, frozenset({"video"})),
    TargetPostType.YOUTUBE_SHORT: (1, 1, frozenset({"video"})),
    TargetPostType.YOUTUBE_POST: (0, 10, frozenset({"image"})),
    TargetPostType.INSTAGRAM_REEL: (1, 1, frozenset({"video"})),
    TargetPostType.INSTAGRAM_IMAGE: (1, 1, frozenset({"image"})),
    TargetPostType.INSTAGRAM_CAROUSEL: (2, 20, frozenset({"image", "video"})),
    TargetPostType.INSTAGRAM_STORY: (1, 1, frozenset({"image", "video"})),
    TargetPostType.TIKTOK_VIDEO: (1, 1, frozenset({"video"})),
    TargetPostType.TIKTOK_DRAFT: (1, 1, frozenset({"video"})),
}

MediaValidator = Callable[[MediaAssetV2, TargetPostType], list[Issue]]


def no_media_validation(_asset: MediaAssetV2, _post_type: TargetPostType) -> list[Issue]:
    return []


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def granted_scopes(account: ConnectedAccount) -> tuple[str, ...]:
    try:
        scopes = json.loads(account.granted_scopes_json or "[]")
    except ValueError:
        return ()
    return tuple(str(scope) for scope in scopes) if isinstance(scopes, list) else ()


def connection_kind(account: ConnectedAccount) -> str:
    """The connection adapter that created the account ("" when unknown)."""
    try:
        metadata = json.loads(account.metadata_json or "{}")
    except ValueError:
        return ""
    connection = metadata.get("connection") if isinstance(metadata, dict) else None
    return str(connection) if connection else ""


def stored_settings(target: DraftTarget) -> dict[str, Any]:
    try:
        raw = json.loads(target.settings_json or "{}")
    except ValueError:
        return {"__invalid__": True}
    return raw if isinstance(raw, dict) else {"__invalid__": True}


@dataclass
class TargetSnapshot:
    target: DraftTarget
    post: ContentDraft
    platform: Platform
    post_type: TargetPostType | None
    account: ConnectedAccount | None
    caption: str
    settings: TargetSettings | None
    effective_settings: dict[str, Any]
    media: list[MediaAssetV2]
    payload: dict[str, Any]
    payload_hash: str
    capability: PublishCapability | None
    publisher: str | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not has_errors(self.issues)

    @property
    def explicit_privacy(self) -> bool:
        if self.post_type is None or self.settings is None:
            return False
        return explicit_privacy(self.post_type, self.settings)


TokenProvider = Callable[[ConnectedAccount], TokenSet]


@dataclass
class SnapshotSources:
    """What the engine needs besides the rows: configuration, adapters, stores, clock.

    `media_stores` maps an asset's `storage` ("local", "r2") to its store;
    `token_provider` returns a fresh token for an account (refreshed under the
    account lock). Both are optional so a build without them still validates.
    """

    settings: Settings
    registry: PublisherRegistry
    clock: Callable[[], datetime]
    media_validator: MediaValidator = no_media_validation
    media_stores: Mapping[str, MediaStore] = field(default_factory=dict)
    token_provider: TokenProvider | None = None

    @property
    def app_status(self) -> AppStatus:
        return AppStatus.from_settings(self.settings)

    @property
    def media_host_available(self) -> bool:
        return self.settings.r2_configured

    @property
    def timezone(self) -> str:
        return self.settings.timezone

    def media_store_for(self, storage: str) -> MediaStore | None:
        return self.media_stores.get(storage)


def post_media(session: Session, post: ContentDraft) -> list[MediaAssetV2]:
    rows = session.execute(
        select(MediaAssetV2)
        .join(DraftMedia, DraftMedia.asset_id == MediaAssetV2.id)
        .where(DraftMedia.draft_id == post.id, MediaAssetV2.workspace_id == post.workspace_id)
        .order_by(DraftMedia.position.asc())
    ).scalars()
    return list(rows)


def account_capability(
    sources: SnapshotSources,
    post_type: TargetPostType,
    account: ConnectedAccount | None,
) -> PublishCapability:
    facts = (
        AccountFacts(
            status=account.status,
            granted_scopes=granted_scopes(account),
            connection=connection_kind(account),
        )
        if account is not None
        else None
    )
    return publishing_capability(
        post_type,
        account=facts,
        app=sources.app_status,
        profile=sources.settings.profile,
        implemented=sources.registry.names(),
        media_host_available=sources.media_host_available,
        mock=sources.registry.mock_mode,
    )


def chosen_publisher(session: Session, account_id: int | None, post_type: str) -> str | None:
    if account_id is None:
        return None
    return session.scalar(
        select(AccountPublisher.publisher).where(
            AccountPublisher.account_id == account_id,
            AccountPublisher.post_type == post_type,
        )
    )


def resolve_publisher(
    session: Session,
    sources: SnapshotSources,
    post_type: TargetPostType,
    account: ConnectedAccount | None,
    capability: PublishCapability,
) -> tuple[str, list[Issue]]:
    """The account's chosen publisher for this post type, or the spec 03 default rule.

    Default: the direct adapter when its capability is ready, otherwise
    `assisted`. An explicit choice that is not ready is an error, never a
    silent switch to another adapter.
    """
    choice = chosen_publisher(session, account.id if account else None, post_type.value)
    if choice is None:
        return capability.default_publisher, []
    if choice == ASSISTED:
        return ASSISTED, []
    publisher = sources.registry.get(choice)
    if publisher is None or not publisher.supports(post_type.value):
        return choice, [
            Issue(
                "error",
                "publisher_unavailable",
                f"the chosen publisher {choice} is not available for {post_type.value}",
                fix="choose another publisher for this account",
            )
        ]
    if choice == capability.direct_adapter and not capability.direct_ready:
        return choice, [
            Issue(
                "error",
                "publisher_not_ready",
                f"{choice} is not ready for this account: {capability.detail}",
                fix="grant the missing permission, or use the checklist (assisted)",
            )
        ]
    return choice, []


def _media_issues(
    sources: SnapshotSources, post_type: TargetPostType, media: list[MediaAssetV2], caption: str
) -> list[Issue]:
    minimum, maximum, kinds = MEDIA_RULES[post_type]
    issues: list[Issue] = []
    count = len(media)
    if post_type is TargetPostType.YOUTUBE_POST and count == 0 and not caption.strip():
        issues.append(
            Issue("error", "post_empty", "a Community post needs text or at least one image")
        )
    if count < minimum or count > maximum:
        expected = str(minimum) if minimum == maximum else f"{minimum} to {maximum}"
        issues.append(
            Issue(
                "error",
                "carousel_item_count"
                if post_type is TargetPostType.INSTAGRAM_CAROUSEL
                else "media_count",
                f"{post_type.value} takes {expected} media item(s); this post has {count}",
            )
        )
    for position, asset in enumerate(media):
        if asset.status in {"uploading", "processing"}:
            issues.append(
                Issue(
                    "error",
                    "media_not_ready",
                    "the media is still being uploaded or checked",
                    item=position,
                )
            )
            continue
        if asset.status != "ready":
            reason = f": {asset.rejected_reason}" if asset.rejected_reason else ""
            issues.append(
                Issue(
                    "error",
                    "media_rejected",
                    f"this media item cannot be used ({asset.status}{reason})",
                    item=position,
                )
            )
            continue
        if asset.kind not in kinds:
            issues.append(
                Issue(
                    "error",
                    "media_kind_not_supported",
                    f"{post_type.value} does not take a {asset.kind or 'file of unknown type'}",
                    item=position,
                )
            )
            continue
        for issue in sources.media_validator(asset, post_type):
            issues.append(
                Issue(
                    issue.severity,
                    issue.code,
                    issue.message,
                    fix=issue.fix,
                    source=issue.source,
                    item=position if issue.item is None else issue.item,
                    field=issue.field,
                )
            )
    return issues


def _schedule_issues(
    sources: SnapshotSources,
    target: DraftTarget,
    post_type: TargetPostType,
    settings: TargetSettings | None,
    media: list[MediaAssetV2],
) -> list[Issue]:
    planned = utc(target.planned_at)
    if planned is None:
        return []
    now = sources.clock()
    issues: list[Issue] = []
    lead = planned - now
    if lead < -MISSED_WINDOW:
        issues.append(
            Issue(
                "error",
                "planned_time_past",
                "the planned time passed more than an hour ago; choose a new time or send now",
                field="planned_at",
            )
        )
        return issues
    if lead <= timedelta(0):
        # "Now", or a time that just passed: it is sent as soon as it is released.
        return issues
    if isinstance(settings, YouTubeVideoSettings) and (settings.schedule_mode or "native") == (
        "native"
    ):
        if lead < NATIVE_MIN_LEAD:
            issues.append(
                Issue(
                    "error",
                    "native_lead_too_short",
                    "YouTube scheduling needs the upload to finish first: plan at least 15 "
                    "minutes ahead, or use held mode",
                    field="planned_at",
                )
            )
        elif lead < NATIVE_WARN_LEAD and sum(a.size_bytes for a in media) >= (
            NATIVE_LARGE_FILE_BYTES
        ):
            issues.append(
                Issue(
                    "warning",
                    "native_lead_short_for_size",
                    "large file: the upload may not finish within the hour before the planned time",
                    field="planned_at",
                )
            )
    return issues


def snapshot(
    session: Session,
    sources: SnapshotSources,
    target: DraftTarget,
    *,
    post: ContentDraft | None = None,
    media: list[MediaAssetV2] | None = None,
) -> TargetSnapshot:
    owner = post or session.get(ContentDraft, target.draft_id)
    if owner is None:
        raise LookupError(f"target {target.id} has no post")
    assets = media if media is not None else post_media(session, owner)
    platform = Platform(target.platform)
    issues: list[Issue] = []
    post_type: TargetPostType | None = None
    try:
        post_type = TargetPostType(target.post_type) if target.post_type else None
    except ValueError:
        post_type = None
    if post_type is None:
        issues.append(
            Issue("error", "post_type_required", "choose what to publish", field="post_type")
        )
    elif platform_of(post_type) is not platform:
        issues.append(
            Issue(
                "error",
                "post_type_mismatch",
                f"{post_type.value} cannot be published to {platform.value}",
                field="post_type",
            )
        )
    account = session.get(ConnectedAccount, target.account_id) if target.account_id else None
    if account is not None and (
        account.workspace_id != target.workspace_id or account.platform != platform.value
    ):
        account = None
    caption = target.caption_override if target.caption_override is not None else owner.body
    settings: TargetSettings | None = None
    effective: dict[str, Any] = {}
    raw = stored_settings(target)
    if post_type is not None:
        try:
            settings = parse_settings(post_type, raw if "__invalid__" not in raw else {})
            if "__invalid__" in raw:
                raise InvalidTargetSettings(post_type.value, [])
        except InvalidTargetSettings:
            issues.append(
                Issue(
                    "error",
                    "settings_invalid",
                    "the stored settings do not fit this post type; set them again",
                    field="settings",
                )
            )
            settings = None
        if settings is not None:
            effective = settings.effective(ai_generated=bool(owner.ai_generated))
            issues.extend(settings_issues(post_type, settings, caption=caption))
        issues.extend(_media_issues(sources, post_type, assets, caption))
        issues.extend(_schedule_issues(sources, target, post_type, settings, assets))
    if account is None:
        issues.append(
            Issue("error", "account_required", "choose a connected account", field="account_id")
        )
    capability: PublishCapability | None = None
    publisher: str | None = None
    if post_type is not None and platform_of(post_type) is platform:
        capability = account_capability(sources, post_type, account)
        publisher, publisher_issues = resolve_publisher(
            session, sources, post_type, account, capability
        )
        issues.extend(publisher_issues)
        if account is not None and account.status != "connected":
            issues.append(
                Issue(
                    "error" if publisher != ASSISTED else "warning",
                    "account_not_connected",
                    f"the account is {account.status}; reconnect it",
                    field="account_id",
                )
            )
    payload = canonical_payload(
        platform=platform.value,
        post_type=post_type.value if post_type else target.post_type,
        account_external_id=account.external_account_id if account else None,
        caption=caption,
        settings=effective,
        media_sha256=[asset.sha256 for asset in assets],
        planned_at=target.planned_at,
    )
    return TargetSnapshot(
        target=target,
        post=owner,
        platform=platform,
        post_type=post_type,
        account=account,
        caption=caption,
        settings=settings,
        effective_settings=effective,
        media=assets,
        payload=payload,
        payload_hash=payload_hash(payload),
        capability=capability,
        publisher=publisher,
        issues=issues,
    )


def issues_as_dicts(issues: list[Issue]) -> list[Mapping[str, object]]:
    return [issue.as_dict() for issue in issues]
