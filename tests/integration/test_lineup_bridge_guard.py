"""The "never both" rule between the Lineup browser publisher and the posts API.

Spec 08 section 2 ("Community posts"): one proposal may reach its channel's
Community tab through exactly one path. These tests pin the claims in both
directions:

- a `youtube_post` target in `cross_post` or `release` refuses a proposal the
  browser path already claimed;
- the browser consume points (`_publishing_preflight`, `_consume_confirmation`,
  `_consume_queued_attempt`) refuse a proposal a released `youtube_post`
  target claimed.

Claims are channel-scoped and fail closed: an account or channel that cannot
be determined counts as "the same channel".
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from runway.auth.principal import local_owner
from runway.auth.problems import Problem
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    Channel,
    ConnectedAccount,
    ContentDraft,
    DraftTarget,
    GenerationRun,
    MediaAsset,
    Proposal,
    PublishAttempt,
    SearchRun,
)
from runway.domain.enums import PublishAttemptStatus
from runway.platforms.capabilities import Platform
from runway.platforms.publish_capability import YOUTUBE_ENGAGE_SCOPE
from runway.platforms.publisher_registry import build_publisher_registry
from runway.platforms.youtube_post import YouTubePostPublisher
from runway.posts.bridge import ProposalBridge
from runway.posts.lineup_guard import (
    api_path_claimed,
    browser_path_claimed,
    channel_conflict,
    claimed_api_targets,
    proposal_for_post,
)
from runway.posts.media import default_media_stores
from runway.posts.media_service import MediaService
from runway.posts.schemas import (
    CrossPostRequest,
    ReleaseRequest,
    TargetInput,
)
from runway.posts.service import PostService
from runway.publishing.youtube import (
    QueuedAttemptInvalidated,
    YouTubeBrowserPublisher,
)

NOW = datetime(2030, 1, 1, 15, 0, tzinfo=UTC)
SLOT = "2030-01-02T10:00:00-05:00"
BROWSER_CHANNEL = "UCbrowser-channel-01"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        publisher_channel_id=BROWSER_CHANNEL,
        youtube_posts_api_access=True,
        youtube_client_id="yt-client",
        youtube_client_secret="yt-secret",
        publishing_mode="authorized_browser",
        publishing_enabled=True,
        youtube_automation_authorized=True,
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture()
def guard_env(tmp_path: Path) -> tuple[Settings, Database]:
    from runway.db import initialize_database

    settings = _settings(tmp_path)
    database = initialize_database(settings)
    yield settings, database
    database.engine.dispose()


def _channel(database: Database, settings: Settings) -> Channel:
    with database.session() as session:
        channel = session.scalar(select(Channel).where(Channel.handle == settings.channel_handle))
        assert channel is not None
        session.expunge(channel)
        return channel


def _proposal(
    database: Database,
    channel: Channel,
    *,
    status: str = "internally_scheduled",
    caption: str = "hello world",
    suffix: str = "a",
) -> int:
    # proposals are unique per (channel, slot): derive a distinct day per suffix.
    slot = f"2030-01-{ord(suffix[0]) - 96:02d}T10:00:00-05:00"
    with database.session() as session:
        search = SearchRun(
            channel_id=channel.id,
            query_plan_json="{}",
            provider="fixture",
            status="done",
        )
        session.add(search)
        session.flush()
        media = MediaAsset(
            kind="image",
            local_path=f"media/lineup-guard-{suffix}.png",
            mime_type="image/png",
            width=100,
            height=100,
            file_size=64,
            sha256=f"{suffix}" * 64,
            perceptual_hash="0" * 64,
        )
        session.add(media)
        session.flush()
        candidate = CandidateImage(
            search_run_id=search.id,
            media_asset_id=media.id,
            search_query="q",
            result_rank=1,
            rights_status="ok",
            quality_score=1.0,
            style_score=1.0,
            novelty_score=1.0,
            caption_potential_score=1.0,
            source_risk_score=0.0,
            final_rank_score=1.0,
        )
        session.add(candidate)
        session.flush()
        run = GenerationRun(
            channel_id=channel.id,
            start_date=date(2030, 1, 1),
            days=1,
            status="done",
        )
        session.add(run)
        session.flush()
        proposal = Proposal(
            generation_run_id=run.id,
            channel_id=channel.id,
            candidate_image_id=candidate.id,
            planned_publish_at=slot,
            scheduled_publish_at=slot,
            recommended_caption=caption,
            alternative_captions_json="[]",
            caption_rationale="",
            final_caption=caption,
            status=status,
            selection_reason="",
            style_score=1.0,
            novelty_score=1.0,
            quality_score=1.0,
        )
        session.add(proposal)
        session.flush()
        return int(proposal.id)


def _attempt(
    database: Database,
    proposal_id: int,
    *,
    status: str = "queued",
    submitted_at: datetime | None = None,
    completed_at: datetime | None = None,
    publisher: str = YouTubeBrowserPublisher.publisher_name,
) -> int:
    now = datetime.now(UTC)
    with database.session() as session:
        token_hash = hashlib.sha256(f"token-{proposal_id}-{status}-{now}".encode()).hexdigest()
        attempt = PublishAttempt(
            proposal_id=proposal_id,
            publisher=publisher,
            status=status,
            confirmation_token_hash=token_hash,
            payload_hash="d" * 64,
            planned_publish_at=SLOT,
            expires_at=now + timedelta(days=1),
            submitted_at=submitted_at,
            completed_at=completed_at,
        )
        session.add(attempt)
        session.flush()
        return int(attempt.id)


def _account(
    database: Database,
    *,
    external_id: str = BROWSER_CHANNEL,
    scopes: tuple[str, ...] = (YOUTUBE_ENGAGE_SCOPE,),
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id=external_id,
            handle="@studio",
            status="connected",
            granted_scopes_json='["' + '","'.join(scopes) + '"]' if scopes else "[]",
            token_ref=f"tok-{external_id}",
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return int(account.id)


def _bridged_target(
    database: Database,
    proposal_id: int,
    *,
    account_id: int | None,
    state: str = "ready",
    caption: str = "hello world",
) -> tuple[int, int]:
    """A `source='proposal'` post with one youtube_post target; returns (post, target)."""
    with database.session() as session:
        post = ContentDraft(
            title="bridged",
            body=caption,
            source="proposal",
            source_ref=str(proposal_id),
        )
        session.add(post)
        session.flush()
        target = DraftTarget(
            draft_id=post.id,
            platform="youtube",
            account_id=account_id,
            post_type="youtube_post",
            planned_at=NOW + timedelta(days=1),
            state=state,
            status=state,
        )
        session.add(target)
        session.flush()
        return int(post.id), int(target.id)


def _post_service(settings: Settings, database: Database) -> PostService:
    return PostService(
        database=database,
        settings=settings,
        registry=build_publisher_registry(settings, database),
        media_stores=default_media_stores(settings),
    )


def _bridge(settings: Settings, database: Database) -> ProposalBridge:
    return ProposalBridge(
        posts=_post_service(settings, database),
        media=MediaService(
            database=database,
            settings=settings,
            stores=default_media_stores(settings),
            registry=build_publisher_registry(settings, database),
        ),
    )


def _proposal_image(settings: Settings, database: Database, proposal_id: int) -> None:
    """The Lineup image `cross_post` copies, as a real PNG under the data dir."""
    with database.session() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        candidate = session.get(CandidateImage, proposal.candidate_image_id)
        assert candidate is not None
        asset = session.get(MediaAsset, candidate.media_asset_id)
        assert asset is not None
        local_path = asset.local_path
    path = settings.resolved_data_dir / local_path
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 20), "red").save(path, format="PNG")


# ------------------------------------------------------------------- queries


def test_channel_conflict_fails_closed_on_unknown_ids() -> None:
    assert channel_conflict("UCa", "UCa")
    assert not channel_conflict("UCa", "UCb")
    assert channel_conflict(None, "UCb")
    assert channel_conflict("UCa", None)
    # The shipped placeholder is not a real channel id: it cannot prove the
    # two paths post to different channels.
    assert channel_conflict("UCa", "UCxxxxxxxxxxxxxxxxxxxxxx")


def test_browser_path_claimed_follows_status_and_unsettled_attempts(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    free = _proposal(database, channel, status="internally_scheduled", suffix="b")
    claimed = _proposal(database, channel, status="externally_scheduled", suffix="c")
    with database.session() as session:
        assert not browser_path_claimed(session, session.get(Proposal, free))
        assert browser_path_claimed(session, session.get(Proposal, claimed))

    finished = _attempt(
        database,
        free,
        status="failed_before_submission",
        submitted_at=NOW,
        completed_at=NOW,
    )
    del finished
    with database.session() as session:
        # A provably pre-submission failure does not claim the proposal.
        assert not browser_path_claimed(session, session.get(Proposal, free))
    _attempt(database, free, status="prepared")
    with database.session() as session:
        assert browser_path_claimed(session, session.get(Proposal, free))


def test_api_path_claimed_is_channel_scoped_and_fails_closed(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, suffix="d")
    account_id = _account(database, external_id="UCother-channel-9")
    _bridged_target(database, proposal_id, account_id=account_id, state="released")

    with database.session() as session:
        # A released target on a provably different channel is no conflict.
        assert not api_path_claimed(session, proposal_id, channel_external_id=BROWSER_CHANNEL)
        # On the target's own channel the same target does claim the proposal.
        assert api_path_claimed(session, proposal_id, channel_external_id="UCother-channel-9")
    post_id, _target_id = _bridged_target(database, proposal_id, account_id=None, state="queued")
    del post_id
    with database.session() as session:
        assert api_path_claimed(session, proposal_id, channel_external_id="UCother-channel-9")


def test_draft_and_failed_api_targets_do_not_claim(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, suffix="e")
    account_id = _account(database)
    # "pending" is the stored value of TargetState.DRAFT.
    for state in ("pending", "ready", "cancelled", "failed_retryable", "failed_final"):
        _bridged_target(
            database,
            proposal_id,
            account_id=account_id,
            state=state,
        )
    with database.session() as session:
        assert claimed_api_targets(session, proposal_id) == []
        assert not api_path_claimed(session, proposal_id, channel_external_id=BROWSER_CHANNEL)


# ------------------------------------------------------------------- bridge


def test_cross_post_refuses_a_youtube_post_target_for_a_browser_claimed_proposal(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="published", suffix="f")
    _proposal_image(settings, database, proposal_id)
    bridge = _bridge(settings, database)

    with pytest.raises(Problem) as caught:
        bridge.cross_post(
            local_owner(),
            proposal_id,
            CrossPostRequest(
                targets=[TargetInput(platform=Platform.youtube, post_type="youtube_post")]
            ),
        )
    assert caught.value.status_code == 409


def test_cross_post_refuses_a_proposal_with_a_live_browser_attempt(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="internally_scheduled", suffix="g")
    _attempt(database, proposal_id, status="prepared")
    _proposal_image(settings, database, proposal_id)
    bridge = _bridge(settings, database)

    with pytest.raises(Problem):
        bridge.cross_post(
            local_owner(),
            proposal_id,
            CrossPostRequest(
                targets=[TargetInput(platform=Platform.youtube, post_type="youtube_post")]
            ),
        )


def test_cross_post_allows_a_proposal_whose_browser_path_is_free(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="approved", suffix="h")
    _attempt(
        database,
        proposal_id,
        status="failed_before_submission",
        submitted_at=NOW,
        completed_at=NOW,
    )
    _proposal_image(settings, database, proposal_id)
    bridge = _bridge(settings, database)

    view = bridge.cross_post(
        local_owner(),
        proposal_id,
        CrossPostRequest(
            targets=[TargetInput(platform=Platform.youtube, post_type="youtube_post")]
        ),
    )
    assert view.source == "proposal"
    assert view.source_ref == str(proposal_id)
    assert view.targets[0].post_type == "youtube_post"


def test_cross_post_allows_other_platforms_on_a_claimed_proposal(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="published", suffix="i")
    _proposal_image(settings, database, proposal_id)
    bridge = _bridge(settings, database)

    view = bridge.cross_post(
        local_owner(),
        proposal_id,
        CrossPostRequest(
            targets=[TargetInput(platform=Platform.instagram, post_type="instagram_image")]
        ),
    )
    assert view.targets[0].post_type == "instagram_image"


# ----------------------------------------------------- browser consume guard


def _browser_publisher(settings: Settings, database: Database) -> YouTubeBrowserPublisher:
    class _Adapter:
        async def schedule(self, post: object) -> object:
            raise AssertionError("the browser must never run in these tests")

    return YouTubeBrowserPublisher(database, settings, _Adapter())  # type: ignore[arg-type]


def test_publishing_preflight_refuses_an_api_claimed_proposal(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="internally_scheduled", suffix="j")
    account_id = _account(database)
    _bridged_target(database, proposal_id, account_id=account_id, state="released")
    publisher = _browser_publisher(settings, database)

    with pytest.raises(ValueError, match="already claimed"):
        publisher._publishing_preflight(proposal_id)


def test_queued_attempt_is_invalidated_when_the_api_claimed_the_proposal(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="internally_scheduled", suffix="k")
    account_id = _account(database)
    _bridged_target(database, proposal_id, account_id=account_id, state="queued")
    attempt_id = _attempt(database, proposal_id, status="queued")
    publisher = _browser_publisher(settings, database)

    with pytest.raises(QueuedAttemptInvalidated):
        publisher._consume_queued_attempt(attempt_id)
    with database.session() as session:
        attempt = session.get(PublishAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == PublishAttemptStatus.INVALIDATED.value


def test_queueing_a_browser_attempt_refuses_an_api_claimed_proposal(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="internally_scheduled", suffix="l")
    account_id = _account(database)
    _bridged_target(database, proposal_id, account_id=account_id, state="sending")
    _proposal_image(settings, database, proposal_id)
    publisher = _browser_publisher(settings, database)

    with pytest.raises(ValueError, match="already claimed"):
        publisher.queue_attempt(proposal_id)


def test_a_free_proposal_still_reaches_the_browser_queue(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="internally_scheduled", suffix="m")
    _proposal_image(settings, database, proposal_id)
    publisher = _browser_publisher(settings, database)

    result = publisher.queue_attempt(proposal_id)
    assert result["status"] == "queued"


# ------------------------------------------------------- adapter guard (API)


def _publisher_ctx(settings: Settings, database: Database, post_id: int, target_id: int) -> object:
    """A minimal PublishContext for the adapter's own guard check."""
    import io

    from runway.platforms.base import TokenSet
    from runway.platforms.publisher import AccountRef, MediaRef, PublishContext

    return PublishContext(
        workspace_id=1,
        post_id=post_id,
        target_id=target_id,
        platform=Platform.youtube,
        post_type="youtube_post",
        publisher="youtube_post",
        account=AccountRef(
            id=1,
            platform=Platform.youtube,
            external_account_id=BROWSER_CHANNEL,
            handle="@studio",
            status="connected",
            granted_scopes=(YOUTUBE_ENGAGE_SCOPE,),
            token_ref="tok-1",
        ),
        title="t",
        caption="hello world",
        settings={},
        media=(
            MediaRef(
                asset_id=1,
                position=0,
                kind="image",
                sha256="a" * 64,
                size_bytes=10,
                content_type="image/png",
                storage="local",
                storage_key="w/1/i.png",
                probe={"width": 800, "height": 600},
            ),
        ),
        planned_at=NOW,
        payload_hash="b" * 64,
        ai_generated=False,
        clock=lambda: NOW,
        _open_media=lambda ref: io.BytesIO(b"x" * 10),
        _access_token=lambda: TokenSet(access_token="a", scopes=[YOUTUBE_ENGAGE_SCOPE], extra={}),
    )


def test_the_adapter_refuses_a_browser_claimed_proposal(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="publishing", suffix="n")
    account_id = _account(database)
    post_id, target_id = _bridged_target(
        database, proposal_id, account_id=account_id, state="ready"
    )
    publisher = YouTubePostPublisher(settings, database)

    issues = publisher.preflight(_publisher_ctx(settings, database, post_id, target_id))  # type: ignore[arg-type]
    assert any(issue.code == "lineup_browser_claimed" for issue in issues)


def test_the_adapter_refuses_a_proposal_claimed_by_another_api_target(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="approved", suffix="o")
    account_id = _account(database)
    _bridged_target(database, proposal_id, account_id=account_id, state="released")
    post_id, target_id = _bridged_target(
        database, proposal_id, account_id=account_id, state="ready"
    )
    publisher = YouTubePostPublisher(settings, database)

    issues = publisher.preflight(_publisher_ctx(settings, database, post_id, target_id))  # type: ignore[arg-type]
    assert any(issue.code == "lineup_claimed" for issue in issues)


def test_the_adapter_allows_a_proposal_no_path_claimed(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="approved", suffix="p")
    account_id = _account(database)
    post_id, target_id = _bridged_target(
        database, proposal_id, account_id=account_id, state="ready"
    )
    publisher = YouTubePostPublisher(settings, database)

    assert publisher.preflight(_publisher_ctx(settings, database, post_id, target_id)) == []  # type: ignore[arg-type]


def test_release_of_a_bridged_target_is_refused_when_the_browser_claimed_it(
    guard_env: tuple[Settings, Database],
) -> None:
    """`release_target` runs the adapter preflight: the guard is enforced there too."""
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, status="externally_scheduled", suffix="q")
    account_id = _account(database)
    post_id, target_id = _bridged_target(
        database, proposal_id, account_id=account_id, state="ready"
    )
    service = _post_service(settings, database)
    with database.session() as session:
        from runway.posts.snapshot import snapshot

        target = session.get(DraftTarget, target_id)
        post = session.get(ContentDraft, post_id)
        assert target is not None and post is not None
        snap = snapshot(session, service.sources, target, post=post)

    with pytest.raises(Problem) as caught:
        service.release_target(
            local_owner(),
            post_id,
            target_id,
            ReleaseRequest(payload_hash=snap.payload_hash, confirmed=True),
        )
    detail = str(caught.value.detail or "") + str(caught.value.extra or "")
    assert "browser" in detail or "claimed" in detail
    with database.session() as session:
        target = session.get(DraftTarget, target_id)
        assert target is not None
        assert target.release_state == "none"


def test_proposal_for_post_resolves_only_bridged_posts(
    guard_env: tuple[Settings, Database],
) -> None:
    settings, database = guard_env
    channel = _channel(database, settings)
    proposal_id = _proposal(database, channel, suffix="r")
    post_id, _target_id = _bridged_target(database, proposal_id, account_id=None)
    with database.session() as session:
        proposal = proposal_for_post(session, post_id)
        assert proposal is not None and proposal.id == proposal_id
        post = ContentDraft(title="plain", body="x")
        session.add(post)
        session.flush()
        assert proposal_for_post(session, post.id) is None
