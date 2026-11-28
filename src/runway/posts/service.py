"""Posts and targets: the one service behind `/api/v1/posts` (spec 03 sections 1, 2, 8).

Every mutation is one transaction that also writes its events and jobs, so a
committed change always has its `target.state_changed` event and a release
always has its send job. Authorization is `require(principal, scope)`; rows
are always looked up inside the principal's workspace.

Invariants enforced here:

- Nothing reaches a platform without a recorded release (invariant 1): only
  `release_target` creates a send job, and only for the exact payload the
  person saw (`payload_hash`) with `confirmed: true`.
- Editing a released target clears the release and cancels its job.
- Privacy is explicit (invariant 13): a target without it stays in `draft`
  and `release_target` refuses it.
- TikTok targets are released only by a person, one at a time (spec 06
  section 4): an API key or a batch gets 403 `human_required`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from runway.api.v1.concurrency import bump_version, require_version
from runway.api.v1.pagination import CursorParams, paginate
from runway.auth.audit import record_audit
from runway.auth.key_limits import acquire_cap_lock, enforce_daily_post_cap
from runway.auth.principal import (
    POSTS_READ,
    POSTS_RELEASE,
    POSTS_SCHEDULE,
    POSTS_WRITE,
    Principal,
    require,
    require_person,
)
from runway.auth.problems import Problem, ProblemCode
from runway.auth.release_policy import authorize_release
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    ContentDraft,
    DraftMedia,
    DraftTarget,
    MediaAssetV2,
    PlatformContent,
    TargetAttempt,
    utcnow,
)
from runway.domain.enums import DraftStatus, TargetState
from runway.domain.state_machine import (
    DRAFT_STATUS_MACHINE,
    TARGET_CANCELLABLE_STATES,
    TARGET_EDITABLE_STATES,
    TARGET_RELEASABLE_STATES,
    TARGET_STATE_MACHINE,
)
from runway.platforms.capabilities import Platform
from runway.platforms.drafts import external_id_candidates
from runway.platforms.post_settings import (
    InvalidTargetSettings,
    parse_settings,
    privacy_in_words,
    release_notice,
)
from runway.platforms.publish_capability import ASSISTED
from runway.platforms.publisher import (
    Issue,
    RateDecision,
    RateGuardContext,
    adapter_prepare_lead,
    adapter_rate_guards,
    has_errors,
)
from runway.platforms.publisher_registry import PublisherRegistry
from runway.posts.context import build_context
from runway.posts.jobs import (
    RECONCILE_JOB,
    SEND_JOB,
    SEND_MAX_ATTEMPTS,
    VERIFY_JOB,
    cancel_pending_jobs,
    enqueue_in,
    free_key,
    release_job_key,
)
from runway.posts.schemas import (
    AttemptOut,
    CapabilityOut,
    IssueOut,
    MarkPublishedRequest,
    MediaRefOut,
    PostCreate,
    PostOut,
    PostPatch,
    ReconcileRequest,
    ReleaseOut,
    ReleaseRequest,
    ScheduleRequest,
    TargetInput,
    TargetOut,
    TargetUpdate,
)
from runway.posts.snapshot import (
    MediaValidator,
    SnapshotSources,
    TargetSnapshot,
    TokenProvider,
    no_media_validation,
    post_media,
    snapshot,
    utc,
)
from runway.posts.states import current_state, derive_post_status, set_target_state
from runway.posts.types import (
    AttemptKind,
    AttemptOutcome,
    PostSource,
    ReleaseState,
    TargetPostType,
    platform_of,
    target_state_name,
)

_T = TargetState

# Schedule rules that depend on the clock. They gate release; at send time the
# worker applies the missed-window rule instead (spec 03 section 6).
SCHEDULE_ISSUE_CODES = frozenset(
    {"planned_time_past", "native_lead_too_short", "native_lead_short_for_size"}
)
# A planned time may not be set further in the past than this (clock skew).
SCHEDULE_PAST_TOLERANCE = timedelta(minutes=1)
YOUTUBE_VIDEO_TYPES = frozenset({TargetPostType.YOUTUBE_VIDEO, TargetPostType.YOUTUBE_SHORT})


def _problem(code: ProblemCode, detail: str, **extra: Any) -> Problem:
    return Problem.of(code, detail, extra=extra or None)


def _issues_problem(detail: str, issues: Sequence[Issue]) -> Problem:
    return _problem(
        ProblemCode.VALIDATION_FAILED,
        detail,
        errors=[issue.as_dict() for issue in issues if issue.severity == "error"],
    )


def principal_name(principal: Principal) -> str:
    return f"{principal.kind}:{principal.id}"


# ------------------------------------------------------------------ loading


def load_post(session: Session, principal: Principal, post_id: int) -> ContentDraft:
    post = session.get(ContentDraft, post_id)
    if post is None or post.workspace_id != principal.workspace_id:
        raise _problem(ProblemCode.NOT_FOUND, "this post does not exist")
    return post


def load_target(session: Session, post: ContentDraft, target_id: int) -> DraftTarget:
    target = session.get(DraftTarget, target_id)
    if target is None or target.draft_id != post.id or target.workspace_id != post.workspace_id:
        raise _problem(ProblemCode.NOT_FOUND, "this target does not exist")
    return target


def post_targets(session: Session, post: ContentDraft) -> list[DraftTarget]:
    return list(
        session.scalars(
            select(DraftTarget).where(DraftTarget.draft_id == post.id).order_by(DraftTarget.id)
        ).all()
    )


def latest_attempt(session: Session, target_id: int) -> TargetAttempt | None:
    return session.scalar(
        select(TargetAttempt)
        .where(TargetAttempt.target_id == target_id)
        .order_by(TargetAttempt.id.desc())
        .limit(1)
    )


# ------------------------------------------------------- validation and release


def revalidate(
    session: Session,
    sources: SnapshotSources,
    target: DraftTarget,
    post: ContentDraft,
    *,
    reason: str | None = None,
) -> TargetSnapshot:
    """Move an unsent target between `draft` and `ready` to match its validation."""
    snap = snapshot(session, sources, target, post=post)
    state = current_state(target)
    if state in {_T.DRAFT, _T.READY}:
        blocking = [
            issue
            for issue in snap.issues
            if issue.severity == "error" and issue.code not in SCHEDULE_ISSUE_CODES
        ]
        desired = _T.DRAFT if blocking else _T.READY
        if desired is not state:
            set_target_state(session, target, desired, reason=reason, post=post)
        elif reason is not None:
            target.state_reason = reason
    return snap


def clear_release(target: DraftTarget) -> None:
    target.release_state = ReleaseState.NONE.value
    target.released_by = None
    target.released_at = None
    target.payload_hash = None


def edit_target(
    session: Session,
    sources: SnapshotSources,
    target: DraftTarget,
    post: ContentDraft,
    *,
    revive_cancelled: bool,
) -> None:
    """After an edit: clear any release, cancel its job and revalidate (spec 03 section 2)."""
    state = current_state(target)
    if state is _T.CANCELLED and not revive_cancelled:
        return
    if state not in TARGET_EDITABLE_STATES:
        return
    was_released = target.release_state == ReleaseState.RELEASED.value
    cancel_pending_jobs(session, target.id)
    clear_release(target)
    if state is not _T.DRAFT and state is not _T.READY:
        snap = snapshot(session, sources, target, post=post)
        blocking = [
            issue
            for issue in snap.issues
            if issue.severity == "error" and issue.code not in SCHEDULE_ISSUE_CODES
        ]
        set_target_state(
            session,
            target,
            _T.DRAFT if blocking else _T.READY,
            reason="edited after release; release it again" if was_released else None,
            post=post,
        )
        return
    revalidate(
        session,
        sources,
        target,
        post,
        reason="edited after release; release it again" if was_released else None,
    )


def _require_editable(target: DraftTarget) -> None:
    state = current_state(target)
    if state not in TARGET_EDITABLE_STATES:
        raise _problem(
            ProblemCode.INVALID_STATE,
            f"this target is {target_state_name(state)}; it cannot be changed now",
            state=target_state_name(state),
        )


def count_sends(
    session: Session,
    *,
    workspace_id: int,
    platform: str,
    account_id: int | None,
    since: datetime,
    scope: str,
) -> int:
    """Send attempts that reached the platform since `since` (for the rate guards)."""
    statement = (
        select(func.count(TargetAttempt.id))
        .join(DraftTarget, DraftTarget.id == TargetAttempt.target_id)
        .where(
            TargetAttempt.kind == AttemptKind.SEND.value,
            TargetAttempt.request_sent_at.is_not(None),
            TargetAttempt.request_sent_at >= since,
            DraftTarget.platform == platform,
            DraftTarget.publisher != ASSISTED,
        )
    )
    if scope == "account":
        statement = statement.where(
            DraftTarget.account_id == account_id, DraftTarget.workspace_id == workspace_id
        )
    return int(session.scalar(statement) or 0)


def count_pending_user(session: Session, *, account_id: int | None, since: datetime) -> int:
    return int(
        session.scalar(
            select(func.count(DraftTarget.id)).where(
                DraftTarget.account_id == account_id,
                DraftTarget.state == _T.NEEDS_USER.value,
                DraftTarget.publisher != ASSISTED,
                DraftTarget.released_at >= since,
            )
        )
        or 0
    )


def check_rate_guards(
    session: Session, sources: SnapshotSources, snap: TargetSnapshot, publisher_name: str
) -> RateDecision:
    publisher = sources.registry.get(publisher_name)
    if publisher is None:
        return RateDecision(True)
    target = snap.target
    now = sources.clock()

    def sent(since: datetime, scope: str) -> int:
        return count_sends(
            session,
            workspace_id=target.workspace_id,
            platform=target.platform,
            account_id=target.account_id,
            since=since,
            scope=scope,
        )

    context = RateGuardContext(
        workspace_id=target.workspace_id,
        account_id=target.account_id,
        platform=snap.platform,
        post_type=str(target.post_type),
        at=now,
        count_sent=sent,
        count_pending_user=lambda since: count_pending_user(
            session, account_id=target.account_id, since=since
        ),
    )
    for guard in adapter_rate_guards(publisher):
        decision = guard(context)
        if not decision.allowed:
            return decision
    return RateDecision(True)


def native_youtube(snap: TargetSnapshot) -> bool:
    return (
        snap.post_type in YOUTUBE_VIDEO_TYPES
        and snap.effective_settings.get("schedule_mode", "native") == "native"
    )


def send_run_at(sources: SnapshotSources, snap: TargetSnapshot, publisher_name: str) -> datetime:
    """When the send job runs (spec 03 section 6).

    YouTube `native` uploads now and lets YouTube publish at `planned_at`;
    an adapter with a `prepare_lead` (Instagram containers) starts that long
    before `planned_at`; everything else runs at `planned_at`.
    """
    now = sources.clock()
    planned = utc(snap.target.planned_at) or now
    if planned <= now or native_youtube(snap):
        return now
    publisher = sources.registry.get(publisher_name)
    lead = adapter_prepare_lead(publisher) if publisher is not None else None
    if lead is not None:
        return max(now, planned - lead)
    return planned


# ------------------------------------------------------------------- views


def _attempt_out(row: TargetAttempt) -> AttemptOut:
    try:
        response = json.loads(row.response_json or "{}")
    except ValueError:
        response = {}
    return AttemptOut(
        id=row.id,
        target_id=row.target_id,
        job_id=row.job_id,
        adapter=row.adapter,
        kind=row.kind,
        payload_hash=row.payload_hash,
        started_at=utc(row.started_at) or row.started_at,
        request_sent_at=utc(row.request_sent_at),
        finished_at=utc(row.finished_at),
        outcome=row.outcome,
        external_id=row.external_id,
        error_code=row.error_code,
        error_detail=row.error_detail,
        response=response if isinstance(response, dict) else {},
    )


def _issue_out(issue: Issue) -> IssueOut:
    return IssueOut(
        severity=issue.severity,
        code=issue.code,
        message=issue.message,
        fix=issue.fix,
        source=issue.source,
        item=issue.item,
        field=issue.field,
    )


def target_view(session: Session, sources: SnapshotSources, snap: TargetSnapshot) -> TargetOut:
    target = snap.target
    state = current_state(target)
    capability = snap.capability
    attempt = latest_attempt(session, target.id)
    errors = [issue for issue in snap.issues if issue.severity == "error"]
    return TargetOut(
        id=target.id,
        post_id=target.draft_id,
        platform=target.platform,
        account_id=target.account_id,
        post_type=target.post_type,
        caption_override=target.caption_override,
        caption=snap.caption,
        settings=snap.settings.model_dump(mode="json") if snap.settings else {},
        effective_settings=snap.effective_settings,
        planned_at=utc(target.planned_at),
        state=target_state_name(state),
        state_reason=target.state_reason,
        version=target.version,
        release=ReleaseOut(
            state="released" if target.release_state == ReleaseState.RELEASED.value else "none",
            released_by=target.released_by,
            released_at=utc(target.released_at),
            payload_hash=target.payload_hash,
        ),
        payload=snap.payload,
        payload_hash=snap.payload_hash,
        publisher=target.publisher
        if target.release_state == ReleaseState.RELEASED.value
        else snap.publisher,
        capability=CapabilityOut.model_validate(capability.as_dict()) if capability else None,
        issues=[_issue_out(issue) for issue in snap.issues],
        releasable=(
            state in TARGET_RELEASABLE_STATES
            and not errors
            and target.planned_at is not None
            and snap.explicit_privacy
        ),
        privacy_in_words=(
            privacy_in_words(snap.post_type, snap.settings)
            if snap.post_type and snap.settings
            else None
        ),
        release_notice=(
            release_notice(snap.post_type, snap.settings)
            if snap.post_type and snap.settings
            else None
        ),
        external_id=target.external_id,
        published_url=target.published_url,
        published_at=utc(target.published_at),
        last_attempt=_attempt_out(attempt) if attempt else None,
    )


def post_view(session: Session, sources: SnapshotSources, post: ContentDraft) -> PostOut:
    assets = post_media(session, post)
    targets = post_targets(session, post)
    snaps = [snapshot(session, sources, target, post=post, media=assets) for target in targets]
    return PostOut(
        id=post.id,
        title=post.title,
        body=post.body,
        notes=post.notes,
        source=post.source,
        source_ref=post.source_ref,
        ai_generated=bool(post.ai_generated),
        created_by=post.created_by,
        version=post.version,
        status=derive_post_status([target.state for target in targets]).value,
        scheduled_for=utc(post.scheduled_for),
        media=[
            MediaRefOut(
                asset_id=asset.id,
                position=position,
                kind=asset.kind,
                status=asset.status,
                sha256=asset.sha256,
                size_bytes=int(asset.size_bytes),
            )
            for position, asset in enumerate(assets)
        ],
        targets=[target_view(session, sources, snap) for snap in snaps],
        created_at=utc(post.created_at) or post.created_at,
        updated_at=utc(post.updated_at) or post.updated_at,
    )


# ------------------------------------------------------------------ service


@dataclass
class PostService:
    database: Database
    settings: Settings
    registry: PublisherRegistry
    clock: Callable[[], datetime] = utcnow
    media_validator: MediaValidator = no_media_validation
    token_provider: TokenProvider | None = None
    media_stores: dict[str, Any] | None = None

    @property
    def sources(self) -> SnapshotSources:
        return SnapshotSources(
            settings=self.settings,
            registry=self.registry,
            clock=self.clock,
            media_validator=self.media_validator,
            media_stores=self.media_stores or {},
            token_provider=self.token_provider,
        )

    # ---------------------------------------------------------------- reading

    def get_post(self, principal: Principal, post_id: int) -> PostOut:
        require(principal, POSTS_READ)
        with self.database.session() as session:
            return post_view(session, self.sources, load_post(session, principal, post_id))

    def list_posts(
        self, principal: Principal, params: CursorParams
    ) -> tuple[list[PostOut], str | None]:
        require(principal, POSTS_READ)
        with self.database.session() as session:
            statement = select(ContentDraft).where(
                ContentDraft.workspace_id == principal.workspace_id,
                ContentDraft.status != DraftStatus.ARCHIVED.value,
            )
            rows, next_cursor = paginate(session, statement, ContentDraft.id, params, scope="posts")
            sources = self.sources
            return [post_view(session, sources, post) for post in rows], next_cursor

    def list_attempts(
        self, principal: Principal, post_id: int, target_id: int, params: CursorParams
    ) -> tuple[list[AttemptOut], str | None]:
        require(principal, POSTS_READ)
        with self.database.session() as session:
            post = load_post(session, principal, post_id)
            target = load_target(session, post, target_id)
            statement = select(TargetAttempt).where(
                TargetAttempt.target_id == target.id,
                TargetAttempt.workspace_id == principal.workspace_id,
            )
            rows, next_cursor = paginate(
                session, statement, TargetAttempt.id, params, scope=f"attempts:{target.id}"
            )
            return [_attempt_out(row) for row in rows], next_cursor

    # ------------------------------------------------------------- creating

    def _media(self, session: Session, principal: Principal, ids: Sequence[int]) -> list[int]:
        if len(set(ids)) != len(ids):
            raise _problem(ProblemCode.VALIDATION_FAILED, "each media item may appear once")
        for asset_id in ids:
            asset = session.get(MediaAssetV2, asset_id)
            if (
                asset is None
                or asset.workspace_id != principal.workspace_id
                or asset.status == "deleted"
            ):
                raise _problem(
                    ProblemCode.VALIDATION_FAILED,
                    f"media {asset_id} does not exist in this workspace",
                )
        return list(ids)

    def _set_media(self, session: Session, post: ContentDraft, ids: Sequence[int]) -> None:
        for row in session.scalars(select(DraftMedia).where(DraftMedia.draft_id == post.id)):
            session.delete(row)
        session.flush()
        for position, asset_id in enumerate(ids):
            session.add(
                DraftMedia(
                    workspace_id=post.workspace_id,
                    draft_id=post.id,
                    asset_id=asset_id,
                    position=position,
                )
            )
        session.flush()

    def _account(
        self, session: Session, principal: Principal, platform: Platform, account_id: int | None
    ) -> int | None:
        if account_id is not None:
            account = session.get(ConnectedAccount, account_id)
            if (
                account is None
                or account.workspace_id != principal.workspace_id
                or account.platform != platform.value
            ):
                raise _problem(
                    ProblemCode.VALIDATION_FAILED,
                    f"account {account_id} is not a connected {platform.value} account here",
                )
            return account_id
        bound = session.scalars(
            select(ConnectedAccount.id).where(
                ConnectedAccount.workspace_id == principal.workspace_id,
                ConnectedAccount.platform == platform.value,
                ConnectedAccount.status == "connected",
            )
        ).all()
        return bound[0] if len(bound) == 1 else None

    @staticmethod
    def _post_type(platform: Platform, post_type: str | None) -> TargetPostType | None:
        if post_type is None:
            return None
        try:
            kind = TargetPostType(post_type)
        except ValueError:
            raise _problem(
                ProblemCode.VALIDATION_FAILED, f"{post_type!r} is not a post type"
            ) from None
        if platform_of(kind) is not platform:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                f"{kind.value} cannot be published to {platform.value}",
            )
        return kind

    @staticmethod
    def _settings_json(
        principal: Principal, kind: TargetPostType | None, settings: dict[str, Any]
    ) -> str:
        if kind is None:
            if settings:
                raise _problem(
                    ProblemCode.VALIDATION_FAILED, "choose a post type before its settings"
                )
            return "{}"
        if kind is TargetPostType.TIKTOK_VIDEO and settings and principal.kind != "user":
            # TikTok's mandatory screen: privacy, interactions and consent are
            # a person's choices (spec 08 section 4, spec 06 section 4).
            raise _problem(
                ProblemCode.HUMAN_REQUIRED,
                "TikTok post settings must be chosen by a person in Runway",
            )
        try:
            parsed = parse_settings(kind, settings)
        except InvalidTargetSettings as exc:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                f"the settings do not fit {kind.value}",
                errors=exc.errors,
            ) from None
        return json.dumps(parsed.model_dump(mode="json", exclude_unset=True), sort_keys=True)

    def _add_target(
        self,
        session: Session,
        principal: Principal,
        post: ContentDraft,
        data: TargetInput,
    ) -> DraftTarget:
        account_id = self._account(session, principal, data.platform, data.account_id)
        duplicate = session.scalar(
            select(DraftTarget.id).where(
                DraftTarget.draft_id == post.id,
                DraftTarget.platform == data.platform.value,
                DraftTarget.account_id == account_id
                if account_id is not None
                else DraftTarget.account_id.is_(None),
            )
        )
        if duplicate is not None:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                f"this post already targets this {data.platform.value} account",
            )
        kind = self._post_type(data.platform, data.post_type)
        planned = utc(data.planned_at) if data.planned_at else utc(post.scheduled_for)
        target = DraftTarget(
            workspace_id=post.workspace_id,
            draft_id=post.id,
            platform=data.platform.value,
            account_id=account_id,
            post_type=kind.value if kind else None,
            caption_override=data.caption_override,
            settings_json=self._settings_json(principal, kind, data.settings),
            planned_at=planned,
            status=TARGET_STATE_MACHINE.start(_T.DRAFT),
            state=TARGET_STATE_MACHINE.start(_T.DRAFT),
            release_state=ReleaseState.NONE.value,
            version=1,
        )
        session.add(target)
        session.flush()
        return target

    def create_post(self, principal: Principal, data: PostCreate) -> PostOut:
        require(principal, POSTS_WRITE)
        with self.database.session() as session:
            post = self.create_post_in(session, principal, data)
            return post_view(session, self.sources, post)

    def create_post_in(
        self,
        session: Session,
        principal: Principal,
        data: PostCreate,
        *,
        source: PostSource | None = None,
        source_ref: str | None = None,
    ) -> ContentDraft:
        """Create a post in the caller's transaction (the proposal bridge uses this).

        The key's daily post cap is enforced here, inside the transaction that
        writes the post (`acquire_cap_lock` serialises it), so every create
        path — `/api/v1/posts`, cross-post, the MCP tool — is counted.
        """
        require(principal, POSTS_WRITE)
        agent = principal.kind == "agent"
        if agent:
            enforce_daily_post_cap(
                session, principal, self.settings.api_key_daily_post_cap, now=self.clock()
            )
        post = ContentDraft(
            workspace_id=principal.workspace_id,
            title=data.title,
            body=data.body,
            scheduled_for=utc(data.scheduled_for),
            notes=data.notes,
            status=DRAFT_STATUS_MACHINE.start(
                DraftStatus.SCHEDULED if data.scheduled_for else DraftStatus.DRAFT
            ),
            source=(source or (PostSource.AGENT if agent else PostSource.HUMAN)).value,
            source_ref=source_ref,
            ai_generated=data.ai_generated if data.ai_generated is not None else agent,
            version=1,
            created_by=principal_name(principal),
        )
        session.add(post)
        session.flush()
        self._set_media(session, post, self._media(session, principal, data.media_ids))
        sources = self.sources
        for item in data.targets:
            target = self._add_target(session, principal, post, item)
            revalidate(session, sources, target, post)
        session.flush()
        return post

    def add_target(self, principal: Principal, post_id: int, data: TargetInput) -> PostOut:
        require(principal, POSTS_WRITE)
        with self.database.session() as session:
            post = load_post(session, principal, post_id)
            target = self._add_target(session, principal, post, data)
            sources = self.sources
            revalidate(session, sources, target, post)
            return post_view(session, sources, post)

    # -------------------------------------------------------------- editing

    def update_post(
        self, principal: Principal, post_id: int, expected_version: int, data: PostPatch
    ) -> PostOut:
        require(principal, POSTS_WRITE)
        with self.database.session() as session:
            post = load_post(session, principal, post_id)
            require_version(post.version, expected_version)
            bump_version(session, ContentDraft.__table__, post.id, expected_version)  # type: ignore[arg-type]
            session.refresh(post)
            changed_payload = False
            if data.title is not None and data.title != post.title:
                post.title = data.title
            if data.body is not None and data.body != post.body:
                post.body = data.body
                changed_payload = True
            if data.notes is not None:
                post.notes = data.notes
            if data.ai_generated is not None and data.ai_generated != post.ai_generated:
                post.ai_generated = data.ai_generated
                changed_payload = True
            if data.media_ids is not None:
                ids = self._media(session, principal, data.media_ids)
                current = [asset.id for asset in post_media(session, post)]
                if ids != current:
                    self._set_media(session, post, ids)
                    changed_payload = True
            if data.clear_schedule:
                post.scheduled_for = None
            elif data.scheduled_for is not None:
                post.scheduled_for = utc(data.scheduled_for)
            session.flush()
            sources = self.sources
            for target in post_targets(session, post):
                if changed_payload:
                    edit_target(session, sources, target, post, revive_cancelled=False)
                else:
                    revalidate(session, sources, target, post)
            return post_view(session, sources, post)

    def update_target(
        self,
        principal: Principal,
        post_id: int,
        target_id: int,
        expected_version: int,
        data: TargetUpdate,
    ) -> PostOut:
        require(principal, POSTS_WRITE)
        with self.database.session() as session:
            post = load_post(session, principal, post_id)
            target = load_target(session, post, target_id)
            require_version(target.version, expected_version)
            _require_editable(target)
            platform = Platform(target.platform)
            kind = self._post_type(platform, data.post_type)
            account_id = (
                self._account(session, principal, platform, data.account_id)
                if data.account_id is not None
                else target.account_id
            )
            if account_id != target.account_id and session.scalar(
                select(DraftTarget.id).where(
                    DraftTarget.draft_id == post.id,
                    DraftTarget.platform == target.platform,
                    DraftTarget.account_id == account_id,
                    DraftTarget.id != target.id,
                )
            ):
                raise _problem(
                    ProblemCode.VALIDATION_FAILED,
                    f"this post already targets this {platform.value} account",
                )
            settings_json = self._settings_json(principal, kind, data.settings)
            bump_version(session, DraftTarget.__table__, target.id, expected_version)  # type: ignore[arg-type]
            session.refresh(target)
            target.post_type = kind.value if kind else None
            target.account_id = account_id
            target.caption_override = data.caption_override
            target.settings_json = settings_json
            session.flush()
            sources = self.sources
            edit_target(session, sources, target, post, revive_cancelled=True)
            return post_view(session, sources, post)

    def schedule_target(
        self,
        principal: Principal,
        post_id: int,
        target_id: int,
        data: ScheduleRequest,
        *,
        expected_version: int | None = None,
    ) -> PostOut:
        require(principal, POSTS_SCHEDULE)
        now = self.clock()
        if data.now == (data.planned_at is not None):
            raise _problem(
                ProblemCode.VALIDATION_FAILED, "send either planned_at or now: true, not both"
            )
        planned = now if data.now else utc(data.planned_at)
        assert planned is not None
        if not data.now and planned < now - SCHEDULE_PAST_TOLERANCE:
            raise _problem(
                ProblemCode.VALIDATION_FAILED, "the planned time is in the past; use now: true"
            )
        with self.database.session() as session:
            post = load_post(session, principal, post_id)
            target = load_target(session, post, target_id)
            if expected_version is not None:
                require_version(target.version, expected_version)
            _require_editable(target)
            target.planned_at = planned
            session.flush()
            sources = self.sources
            edit_target(session, sources, target, post, revive_cancelled=True)
            return post_view(session, sources, post)

    # --------------------------------------------------------------- release

    def release_target(
        self,
        principal: Principal,
        post_id: int,
        target_id: int,
        data: ReleaseRequest,
        *,
        batch: bool = False,
    ) -> PostOut:
        """Approve this exact payload and queue its one send job (spec 03 sections 2, 4)."""
        require(principal, POSTS_RELEASE)
        with self.database.session() as session:
            if principal.kind == "agent":
                # Lock before the policy/grant/cap reads so two releases by the
                # same key cannot both pass the daily cap (check-then-act).
                acquire_cap_lock(session, principal)
            post = load_post(session, principal, post_id)
            target = load_target(session, post, target_id)
            # The request-policy guard checks release routes too; the grant and
            # daily-cap count must also happen inside this transaction.
            authorize_release(session, principal, platform=target.platform)
            if target.platform == Platform.tiktok.value and (principal.kind != "user" or batch):
                raise _problem(
                    ProblemCode.HUMAN_REQUIRED,
                    "TikTok posts are released by a person, one at a time, in Runway",
                    platform="tiktok",
                )
            state = current_state(target)
            if state not in TARGET_RELEASABLE_STATES:
                raise _problem(
                    ProblemCode.INVALID_STATE,
                    f"this target is {target_state_name(state)}; only a ready target can be "
                    "released",
                    state=target_state_name(state),
                )
            if not data.confirmed:
                raise _problem(
                    ProblemCode.VALIDATION_FAILED, "confirm the release (confirmed: true)"
                )
            sources = self.sources
            snap = snapshot(session, sources, target, post=post)
            if data.payload_hash != snap.payload_hash:
                raise _problem(
                    ProblemCode.VERSION_CONFLICT,
                    "the post changed since you reviewed it; review the payload again",
                    current_payload_hash=snap.payload_hash,
                )
            if not snap.explicit_privacy:
                raise _problem(
                    ProblemCode.VALIDATION_FAILED,
                    "choose the privacy setting explicitly before releasing",
                    errors=[{"code": "privacy_required", "field": "settings"}],
                )
            if has_errors(snap.issues):
                raise _issues_problem("this target has errors to fix first", snap.issues)
            if target.platform == Platform.tiktok.value and snap.post_type and snap.settings:
                # TikTok's mandatory screen ends in an express consent; the
                # release echoes the exact line the dialog showed (spec 08 §4).
                notice = release_notice(snap.post_type, snap.settings)
                if notice and data.consent != notice:
                    raise _problem(
                        ProblemCode.VALIDATION_FAILED,
                        "repeat the consent text shown for this release",
                        errors=[{"code": "consent_echo_required", "field": "consent"}],
                    )
            if target.planned_at is None:
                raise _problem(
                    ProblemCode.VALIDATION_FAILED,
                    "schedule the target first (a time, or now)",
                    errors=[{"code": "planned_at_required", "field": "planned_at"}],
                )
            publisher_name = snap.publisher or ASSISTED
            publisher = sources.registry.get(publisher_name)
            if publisher is None:
                raise _problem(
                    ProblemCode.CAPABILITY_NOT_READY,
                    f"the publisher {publisher_name} is not available in this build",
                )
            preflight = publisher.preflight(build_context(sources, snap, publisher=publisher_name))
            if has_errors(preflight):
                raise _issues_problem("the publisher's checks did not pass", preflight)
            decision = check_rate_guards(session, sources, snap, publisher_name)
            if not decision.allowed:
                raise _problem(
                    ProblemCode.RATE_LIMITED,
                    decision.detail or "the platform's posting limit is reached",
                    retry_at=decision.retry_at.isoformat() if decision.retry_at else None,
                )
            now = self.clock()
            target.release_state = ReleaseState.RELEASED.value
            target.released_by = principal_name(principal)
            target.released_at = now
            target.payload_hash = snap.payload_hash
            target.publisher = publisher_name
            set_target_state(session, target, _T.RELEASED, post=post)
            job = enqueue_in(
                session,
                SEND_JOB,
                idempotency_key=free_key(session, release_job_key(target.id, snap.payload_hash)),
                run_at=send_run_at(sources, snap, publisher_name),
                workspace_id=target.workspace_id,
                target_id=target.id,
                payload={"payload_hash": snap.payload_hash, "publisher": publisher_name},
                max_attempts=SEND_MAX_ATTEMPTS,
            )
            set_target_state(
                session,
                target,
                _T.QUEUED,
                post=post,
                event_payload={"job_id": job.id, "run_at": utc(job.run_at).isoformat()},  # type: ignore[union-attr]
            )
            record_audit(
                session,
                principal,
                "post.release",
                target_type="target",
                target_id=target.id,
                detail={
                    "post_id": post.id,
                    "platform": target.platform,
                    "payload_hash": snap.payload_hash,
                },
            )
            return post_view(session, sources, post)

    def cancel_target(self, principal: Principal, post_id: int, target_id: int) -> PostOut:
        require(principal, POSTS_SCHEDULE)
        with self.database.session() as session:
            post = load_post(session, principal, post_id)
            target = load_target(session, post, target_id)
            state = current_state(target)
            assisted_wait = state is _T.NEEDS_USER and target.publisher == ASSISTED
            if state not in TARGET_CANCELLABLE_STATES and not assisted_wait:
                raise _problem(
                    ProblemCode.INVALID_STATE,
                    f"this target is {target_state_name(state)}; it can only be cancelled "
                    "before it is sent",
                    state=target_state_name(state),
                )
            cancel_pending_jobs(session, target.id)
            clear_release(target)
            set_target_state(session, target, _T.CANCELLED, reason="cancelled", post=post)
            return post_view(session, self.sources, post)

    # ------------------------------------------------------- resolving outcomes

    def reconcile_target(
        self, principal: Principal, post_id: int, target_id: int, data: ReconcileRequest
    ) -> PostOut:
        require(principal, POSTS_RELEASE)
        with self.database.session() as session:
            post = load_post(session, principal, post_id)
            target = load_target(session, post, target_id)
            if principal.kind == "agent":
                # Reconciling decides what reached a platform: release-adjacent,
                # so a key needs the same grant as a release (invariant 16).
                authorize_release(session, principal, platform=target.platform)
            state = current_state(target)
            if state is not _T.UNCERTAIN:
                raise _problem(
                    ProblemCode.INVALID_STATE,
                    f"only an uncertain target can be reconciled; this one is "
                    f"{target_state_name(state)}",
                    state=target_state_name(state),
                )
            now = self.clock()
            if data.resolution == "not_published":
                if not data.confirmed:
                    raise _problem(
                        ProblemCode.VALIDATION_FAILED,
                        "confirm that you checked the platform (confirmed: true)",
                    )
                session.add(
                    TargetAttempt(
                        workspace_id=target.workspace_id,
                        target_id=target.id,
                        adapter="person",
                        kind=AttemptKind.RECONCILE.value,
                        payload_hash=target.payload_hash,
                        started_at=now,
                        finished_at=now,
                        outcome=AttemptOutcome.ABSENT.value,
                        request_json=json.dumps({"by": principal_name(principal)}),
                        response_json=json.dumps({"resolution": "not_published"}),
                    )
                )
                cancel_pending_jobs(session, target.id)
                set_target_state(
                    session,
                    target,
                    _T.FAILED_FINAL,
                    reason="a person checked the platform: it was not published; release "
                    "again to retry",
                    post=post,
                )
                return post_view(session, self.sources, post)
            enqueue_in(
                session,
                RECONCILE_JOB,
                idempotency_key=free_key(session, f"reconcile:{target.id}:human"),
                run_at=now,
                workspace_id=target.workspace_id,
                target_id=target.id,
                payload={"requested_by": principal_name(principal)},
            )
            target.state_reason = "checking the platform again"
            return post_view(session, self.sources, post)

    def mark_published(
        self, principal: Principal, post_id: int, target_id: int, data: MarkPublishedRequest
    ) -> PostOut:
        """The assisted adapter's "mark as published", and manual resolution."""
        require(principal, POSTS_RELEASE)
        # Attesting "this reached the platform" is a person's statement; a key
        # can never make it (invariant 16).
        require_person(principal, "mark a post as published")
        if urlsplit(data.published_url).scheme not in {"http", "https"}:
            raise _problem(ProblemCode.VALIDATION_FAILED, "published_url must use http or https")
        with self.database.session() as session:
            post = load_post(session, principal, post_id)
            target = load_target(session, post, target_id)
            state = current_state(target)
            if not TARGET_STATE_MACHINE.allowed(state, _T.PUBLISHED) or state is _T.SENDING:
                raise _problem(
                    ProblemCode.INVALID_STATE,
                    f"this target is {target_state_name(state)}; it cannot be marked published",
                    state=target_state_name(state),
                )
            if state is _T.PUBLISHED:
                raise _problem(ProblemCode.INVALID_STATE, "this target is already published")
            candidates = external_id_candidates(target.platform, data.published_url)
            external_id = data.external_id or (candidates[1] if len(candidates) > 1 else None)
            match = session.scalar(
                select(PlatformContent).where(
                    PlatformContent.workspace_id == target.workspace_id,
                    PlatformContent.platform == target.platform,
                    or_(
                        PlatformContent.permalink == data.published_url,
                        PlatformContent.external_id.in_(
                            [external_id, *candidates] if external_id else candidates
                        ),
                    ),
                )
            )
            now = self.clock()
            cancel_pending_jobs(session, target.id)
            publisher = target.publisher or ASSISTED
            if target.publisher is None:
                target.publisher = ASSISTED
            session.add(
                TargetAttempt(
                    workspace_id=target.workspace_id,
                    target_id=target.id,
                    adapter=publisher,
                    kind=AttemptKind.SEND.value
                    if state is _T.NEEDS_USER
                    else AttemptKind.RECONCILE.value,
                    payload_hash=target.payload_hash,
                    started_at=now,
                    finished_at=now,
                    outcome=AttemptOutcome.PUBLISHED.value,
                    external_id=external_id or (match.external_id if match else None),
                    request_json=json.dumps({"by": principal_name(principal), "manual": True}),
                    response_json=json.dumps({"published_url": data.published_url}),
                )
            )
            target.published_url = data.published_url
            target.published_at = now
            target.external_id = external_id or (match.external_id if match else None)
            target.published_content_id = match.id if match else None
            set_target_state(
                session,
                target,
                _T.PUBLISHED,
                reason=f"marked published by {principal.display}",
                post=post,
            )
            enqueue_in(
                session,
                VERIFY_JOB,
                idempotency_key=free_key(session, f"verify:{target.id}:manual"),
                run_at=now,
                workspace_id=target.workspace_id,
                target_id=target.id,
                payload={"try": 1},
            )
            return post_view(session, self.sources, post)
