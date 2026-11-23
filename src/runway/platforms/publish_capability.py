"""Per-account publishing capability (spec 08 section 5, invariant 14).

Whether a post type can go out through its direct adapter depends on the
account (status, granted scopes), the operator's app-approval status, the
profile (whose account it is) and whether this build has the adapter. The
manual `assisted` adapter is always available, so every state except
`ready` / `ready_draft_only` means "use the checklist".

`docs/PLATFORM_CAPABILITIES.md` is generated from this module by
`scripts/generate_platform_capabilities.py`; a test fails when they drift.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Literal

from runway.config import Settings
from runway.platforms.capabilities import YOUTUBE_ENGAGE_SCOPE, Platform
from runway.posts.types import TargetPostType, platform_of

PublishingState = Literal[
    "ready",
    "ready_draft_only",
    "needs_scope",
    "needs_media_host",
    "needs_app_approval",
    "assisted_only",
]
PUBLISHING_STATES: tuple[PublishingState, ...] = (
    "ready",
    "ready_draft_only",
    "needs_scope",
    "needs_media_host",
    "needs_app_approval",
    "assisted_only",
)
STATE_MEANINGS: dict[PublishingState, str] = {
    "ready": "Adapter implemented, scope granted, app status sufficient",
    "ready_draft_only": "TikTok inbox: the draft lands in the creator's inbox; they post it",
    "needs_scope": "The account must grant the publishing scope (or be reconnected)",
    "needs_media_host": "Instagram in a local install without R2: no public media URL",
    "needs_app_approval": "Platform review not passed for this kind of account",
    "assisted_only": "Manual workflow (checklist, then mark as published)",
}

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"
INSTAGRAM_PUBLISH_SCOPE = "instagram_business_content_publish"
TIKTOK_PUBLISH_SCOPE = "video.publish"
TIKTOK_UPLOAD_SCOPE = "video.upload"

ASSISTED = "assisted"

# The direct adapter that serves each post type when this build has it.
DIRECT_ADAPTERS: dict[TargetPostType, str] = {
    TargetPostType.YOUTUBE_VIDEO: "youtube_direct",
    TargetPostType.YOUTUBE_SHORT: "youtube_direct",
    TargetPostType.YOUTUBE_POST: "youtube_post",
    TargetPostType.INSTAGRAM_REEL: "instagram_direct",
    TargetPostType.INSTAGRAM_IMAGE: "instagram_direct",
    TargetPostType.INSTAGRAM_CAROUSEL: "instagram_direct",
    TargetPostType.INSTAGRAM_STORY: "instagram_direct",
    TargetPostType.TIKTOK_VIDEO: "tiktok_direct",
    TargetPostType.TIKTOK_DRAFT: "tiktok_inbox",
}

# Any one of these grants the post type's publishing permission.
REQUIRED_SCOPES: dict[TargetPostType, tuple[str, ...]] = {
    TargetPostType.YOUTUBE_VIDEO: (
        YOUTUBE_UPLOAD_SCOPE,
        YOUTUBE_MANAGE_SCOPE,
        YOUTUBE_ENGAGE_SCOPE,
    ),
    TargetPostType.YOUTUBE_SHORT: (
        YOUTUBE_UPLOAD_SCOPE,
        YOUTUBE_MANAGE_SCOPE,
        YOUTUBE_ENGAGE_SCOPE,
    ),
    TargetPostType.YOUTUBE_POST: (YOUTUBE_ENGAGE_SCOPE, YOUTUBE_MANAGE_SCOPE),
    TargetPostType.INSTAGRAM_REEL: (INSTAGRAM_PUBLISH_SCOPE,),
    TargetPostType.INSTAGRAM_IMAGE: (INSTAGRAM_PUBLISH_SCOPE,),
    TargetPostType.INSTAGRAM_CAROUSEL: (INSTAGRAM_PUBLISH_SCOPE,),
    TargetPostType.INSTAGRAM_STORY: (INSTAGRAM_PUBLISH_SCOPE,),
    TargetPostType.TIKTOK_VIDEO: (TIKTOK_PUBLISH_SCOPE,),
    TargetPostType.TIKTOK_DRAFT: (TIKTOK_UPLOAD_SCOPE,),
}


@dataclass(frozen=True, slots=True)
class AccountFacts:
    """What the capability rule reads about one connected account."""

    status: str
    granted_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AppStatus:
    youtube: str
    instagram: str
    tiktok: str
    youtube_posts_api_access: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> AppStatus:
        return cls(
            youtube=settings.youtube_app_status,
            instagram=settings.instagram_app_status,
            tiktok=settings.tiktok_app_status,
            youtube_posts_api_access=settings.youtube_posts_api_access,
        )


@dataclass(frozen=True, slots=True)
class PublishCapability:
    post_type: TargetPostType
    state: PublishingState
    detail: str
    direct_adapter: str
    # The adapter used when nobody chose one: direct when ready, else assisted.
    default_publisher: str
    missing_scopes: tuple[str, ...] = ()
    available_publishers: tuple[str, ...] = field(default=(ASSISTED,))

    @property
    def direct_ready(self) -> bool:
        return self.state in {"ready", "ready_draft_only"}

    def as_dict(self) -> dict[str, object]:
        return {
            "post_type": self.post_type.value,
            "state": self.state,
            "detail": self.detail,
            "direct_adapter": self.direct_adapter,
            "default_publisher": self.default_publisher,
            "missing_scopes": list(self.missing_scopes),
            "available_publishers": list(self.available_publishers),
        }


def _app_gate(
    post_type: TargetPostType, *, profile: str, app: AppStatus
) -> tuple[bool, str] | None:
    """None when the app status allows this account; else (blocked, reason)."""
    platform = platform_of(post_type)
    if post_type is TargetPostType.YOUTUBE_POST and not app.youtube_posts_api_access:
        return True, (
            "the YouTube posts API is a pilot; set youtube_posts_api_access once Google "
            "admits the project (the Lineup browser path stays available)"
        )
    if post_type is TargetPostType.TIKTOK_VIDEO and app.tiktok != "audited":
        return True, (
            f"TikTok direct posting needs an audited app (now {app.tiktok}); unaudited apps "
            "can only post privately, so use the inbox draft or the checklist"
        )
    if profile == "local":
        # Owner install: the owner's accounts have a role on the owner's apps.
        return None
    if platform is Platform.youtube and app.youtube == "unverified":
        return True, "the Google app is unverified; hosted publishing needs verification"
    if platform is Platform.instagram and app.instagram != "live_advanced":
        return True, (
            f"Instagram needs Advanced Access for accounts without a role on the app "
            f"(now {app.instagram})"
        )
    if post_type is TargetPostType.TIKTOK_DRAFT and app.tiktok == "sandbox":
        return True, "a TikTok sandbox app serves only its own test accounts"
    return None


def publishing_capability(
    post_type: TargetPostType | str,
    *,
    account: AccountFacts | None,
    app: AppStatus,
    profile: str,
    implemented: Collection[str],
    media_host_available: bool,
    mock: bool = False,
) -> PublishCapability:
    """The publishing state of one post type for one account (spec 08 section 5)."""
    kind = TargetPostType(post_type)
    direct = DIRECT_ADAPTERS[kind]
    draft_only = kind is TargetPostType.TIKTOK_DRAFT

    def result(
        state: PublishingState, detail: str, missing: Sequence[str] = ()
    ) -> PublishCapability:
        ready = state in {"ready", "ready_draft_only"}
        return PublishCapability(
            post_type=kind,
            state=state,
            detail=detail,
            direct_adapter=direct,
            default_publisher=direct if ready else ASSISTED,
            missing_scopes=tuple(missing),
            available_publishers=(direct, ASSISTED) if ready else (ASSISTED,),
        )

    if direct not in implemented:
        return result("assisted_only", f"{direct} is not available in this build")
    if mock:
        return result(
            "ready_draft_only" if draft_only else "ready",
            "mock publisher: walks the full state machine without any network call",
        )
    gate = _app_gate(kind, profile=profile, app=app)
    if gate is not None:
        return result("needs_app_approval", gate[1])
    if account is None:
        return result("needs_scope", "choose a connected account")
    if account.status != "connected":
        return result("needs_scope", f"the account is {account.status}; reconnect it")
    wanted = REQUIRED_SCOPES[kind]
    if not set(wanted) & set(account.granted_scopes):
        return result(
            "needs_scope",
            f"turn on publishing for this account to grant {wanted[0]}",
            missing=(wanted[0],),
        )
    if platform_of(kind) is Platform.instagram and not media_host_available:
        return result(
            "needs_media_host",
            "Instagram fetches media from a public URL; configure R2 (RUNWAY_R2_*) or use "
            "the checklist",
        )
    if draft_only:
        return result(
            "ready_draft_only",
            "drafts go to the creator's TikTok inbox (at most 5 waiting per 24 hours)",
        )
    return result("ready", f"{direct} is ready")


def account_publishing_capabilities(
    platform: Platform,
    *,
    account: AccountFacts | None,
    app: AppStatus,
    profile: str,
    implemented: Collection[str],
    media_host_available: bool,
    mock: bool = False,
) -> list[PublishCapability]:
    return [
        publishing_capability(
            post_type,
            account=account,
            app=app,
            profile=profile,
            implemented=implemented,
            media_host_available=media_host_available,
            mock=mock,
        )
        for post_type in TargetPostType
        if platform_of(post_type) is platform
    ]


# --------------------------------------------------------------- documentation

_SCENARIOS: tuple[tuple[str, str, AppStatus, bool, bool], ...] = (
    # (label, profile, app status, scope granted, media host)
    (
        "Owner install, publishing scope granted, no R2",
        "local",
        AppStatus("unverified", "development", "sandbox", False),
        True,
        False,
    ),
    (
        "Owner install, scope granted, R2 configured",
        "local",
        AppStatus("unverified", "development", "sandbox", False),
        True,
        True,
    ),
    (
        "Owner install, scope not granted",
        "local",
        AppStatus("unverified", "development", "sandbox", False),
        False,
        True,
    ),
    (
        "Hosted, default app status",
        "hosted",
        AppStatus("unverified", "development", "sandbox", False),
        True,
        True,
    ),
    (
        "Hosted, all approvals (verified, live_advanced, audited, posts pilot)",
        "hosted",
        AppStatus("verified", "live_advanced", "audited", True),
        True,
        True,
    ),
)


def capability_document(implemented_in_build: Collection[str]) -> str:
    """The generated section of docs/PLATFORM_CAPABILITIES.md."""
    lines = [
        "## Publishing capability per account (generated)",
        "",
        "Generated from `runway.platforms.publish_capability` by",
        "`scripts/generate_platform_capabilities.py`; do not edit by hand. A test fails when",
        "this section and the code disagree.",
        "",
        "Publishing is a function of the account (status, granted scopes), the operator's",
        "app status (`RUNWAY_YOUTUBE_APP_STATUS`, `RUNWAY_INSTAGRAM_APP_STATUS`,",
        "`RUNWAY_TIKTOK_APP_STATUS`, `RUNWAY_YOUTUBE_POSTS_API_ACCESS`), the profile and the",
        "adapters in the build. The `assisted` checklist adapter is always available.",
        "",
        "| State | Meaning |",
        "| --- | --- |",
    ]
    lines.extend(f"| `{state}` | {STATE_MEANINGS[state]} |" for state in PUBLISHING_STATES)
    lines.extend(
        [
            "",
            "Direct adapters and the scopes that enable them:",
            "",
            "| Post type | Direct adapter | In this build | Scope (any of) |",
            "| --- | --- | --- | --- |",
        ]
    )
    for post_type in TargetPostType:
        adapter = DIRECT_ADAPTERS[post_type]
        built = "yes" if adapter in implemented_in_build else "no (assisted only)"
        scopes = ", ".join(f"`{scope}`" for scope in REQUIRED_SCOPES[post_type])
        lines.append(f"| `{post_type.value}` | `{adapter}` | {built} | {scopes} |")
    lines.extend(
        [
            "",
            "State once the direct adapter is installed, for a connected account:",
            "",
            "| Post type | " + " | ".join(label for label, *_ in _SCENARIOS) + " |",
            "| --- |" + " --- |" * len(_SCENARIOS),
        ]
    )
    every_adapter = set(DIRECT_ADAPTERS.values())
    for post_type in TargetPostType:
        cells: list[str] = []
        for _label, profile, app, granted, media_host in _SCENARIOS:
            granted_scopes = REQUIRED_SCOPES[post_type][:1] if granted else ()
            capability = publishing_capability(
                post_type,
                account=AccountFacts(status="connected", granted_scopes=granted_scopes),
                app=app,
                profile=profile,
                implemented=every_adapter,
                media_host_available=media_host,
            )
            cells.append(f"`{capability.state}`")
        lines.append(f"| `{post_type.value}` | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)
