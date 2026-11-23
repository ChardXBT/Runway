"""Publisher port, registry, assisted and mock adapters, capability (spec 03 §5, 08 §5-6)."""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from runway.config import Settings
from runway.db.base import Database
from runway.platforms.assisted_publisher import AssistedPublisher
from runway.platforms.base import ProviderError
from runway.platforms.capabilities import Platform
from runway.platforms.mock_publishers import MockPublisher, mock_publishers
from runway.platforms.publish_capability import (
    INSTAGRAM_PUBLISH_SCOPE,
    TIKTOK_PUBLISH_SCOPE,
    TIKTOK_UPLOAD_SCOPE,
    YOUTUBE_UPLOAD_SCOPE,
    AccountFacts,
    AppStatus,
    publishing_capability,
)
from runway.platforms.publisher import (
    NullProgress,
    PublishAuthRevoked,
    PublishContext,
    Publisher,
    PublishRefused,
    PublishRetryLater,
    classify_send_failure,
)
from runway.platforms.publisher_registry import (
    LIVE_PUBLISHER_NAMES,
    DuplicatePublisher,
    PublisherRegistry,
    build_publisher_registry,
)
from runway.posts.types import TargetPostType

ROOT = Path(__file__).resolve().parents[2]
OWNER = AppStatus("unverified", "development", "sandbox", False)
EVERY_ADAPTER = {
    "youtube_direct",
    "youtube_post",
    "instagram_direct",
    "tiktok_direct",
    "tiktok_inbox",
    "assisted",
}


def _capability(
    post_type: TargetPostType,
    scopes: tuple[str, ...],
    *,
    app: AppStatus = OWNER,
    profile: str = "local",
    media_host: bool = False,
    implemented: set[str] | None = None,
    status: str = "connected",
) -> str:
    return publishing_capability(
        post_type,
        account=AccountFacts(status=status, granted_scopes=scopes),
        app=app,
        profile=profile,
        implemented=EVERY_ADAPTER if implemented is None else implemented,
        media_host_available=media_host,
    ).state


def test_capability_is_per_account() -> None:
    video = TargetPostType.YOUTUBE_VIDEO
    assert _capability(video, (YOUTUBE_UPLOAD_SCOPE,)) == "ready"
    assert _capability(video, ()) == "needs_scope"
    assert _capability(video, (YOUTUBE_UPLOAD_SCOPE,), status="needs_reauth") == "needs_scope"
    assert _capability(video, (YOUTUBE_UPLOAD_SCOPE,), implemented={"assisted"}) == (
        "assisted_only"
    )
    assert _capability(video, (YOUTUBE_UPLOAD_SCOPE,), profile="hosted") == "needs_app_approval"


def test_instagram_in_a_local_install_needs_a_media_host() -> None:
    reel = TargetPostType.INSTAGRAM_REEL
    assert _capability(reel, (INSTAGRAM_PUBLISH_SCOPE,)) == "needs_media_host"
    assert _capability(reel, (INSTAGRAM_PUBLISH_SCOPE,), media_host=True) == "ready"


def test_tiktok_direct_is_never_offered_below_audited() -> None:
    for status in ("sandbox", "production"):
        app = AppStatus("unverified", "development", status, False)
        assert _capability(TargetPostType.TIKTOK_VIDEO, (TIKTOK_PUBLISH_SCOPE,), app=app) == (
            "needs_app_approval"
        )
    audited = AppStatus("unverified", "development", "audited", False)
    assert _capability(TargetPostType.TIKTOK_VIDEO, (TIKTOK_PUBLISH_SCOPE,), app=audited) == (
        "ready"
    )
    assert _capability(TargetPostType.TIKTOK_DRAFT, (TIKTOK_UPLOAD_SCOPE,)) == "ready_draft_only"


def test_youtube_posts_api_is_gated_by_the_pilot_setting() -> None:
    scope = ("https://www.googleapis.com/auth/youtube.force-ssl",)
    assert _capability(TargetPostType.YOUTUBE_POST, scope) == "needs_app_approval"
    piloted = AppStatus("unverified", "development", "sandbox", True)
    assert _capability(TargetPostType.YOUTUBE_POST, scope, app=piloted) == "ready"


def test_default_publisher_is_direct_when_ready_else_assisted() -> None:
    ready = publishing_capability(
        TargetPostType.YOUTUBE_VIDEO,
        account=AccountFacts("connected", (YOUTUBE_UPLOAD_SCOPE,)),
        app=OWNER,
        profile="local",
        implemented=EVERY_ADAPTER,
        media_host_available=False,
    )
    assert ready.default_publisher == "youtube_direct"
    blocked = publishing_capability(
        TargetPostType.YOUTUBE_VIDEO,
        account=AccountFacts("connected", ()),
        app=OWNER,
        profile="local",
        implemented=EVERY_ADAPTER,
        media_host_available=False,
    )
    assert blocked.default_publisher == "assisted"
    assert blocked.available_publishers == ("assisted",)


def test_app_status_settings_default_to_the_owner_install(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    assert AppStatus.from_settings(settings) == OWNER


def test_capability_document_has_not_drifted() -> None:
    spec = importlib.util.spec_from_file_location(
        "generate_platform_capabilities", ROOT / "scripts" / "generate_platform_capabilities.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = module.DOCUMENT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert module.render(current) == current, (
        "docs/PLATFORM_CAPABILITIES.md is out of date: run "
        "python scripts/generate_platform_capabilities.py"
    )


# ---------------------------------------------------------------- registry


def test_live_build_registers_exactly_the_live_names(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    registry = build_publisher_registry(settings, Database(settings))
    assert registry.names() == LIVE_PUBLISHER_NAMES
    assert "assisted" in registry
    assert not registry.mock_mode


def test_mock_publishers_only_exist_in_mock_mode(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    registry = build_publisher_registry(settings, Database(settings))
    assert registry.names() == EVERY_ADAPTER
    for post_type in TargetPostType:
        assert len(registry.supporting(post_type.value)) == 2  # its mock and assisted
    live = PublisherRegistry()
    with pytest.raises(ValueError, match="mock mode"):
        live.register(mock_publishers(settings)[0])


def test_registry_refuses_duplicates_and_non_publishers(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    registry = PublisherRegistry()
    registry.register(AssistedPublisher(Database(settings)))
    with pytest.raises(DuplicatePublisher):
        registry.register(AssistedPublisher(Database(settings)))
    with pytest.raises(TypeError):
        registry.register(object())  # type: ignore[arg-type]
    assert isinstance(registry.require("assisted"), Publisher)


# ------------------------------------------------------- failure classification


def test_failure_classification_follows_the_point_of_no_return() -> None:
    connect = httpx.ConnectError("refused")
    timeout = httpx.ReadTimeout("slow")
    assert classify_send_failure(connect, request_sent=True).state == "failed_retryable"
    assert classify_send_failure(timeout, request_sent=True).state == "uncertain"
    assert classify_send_failure(timeout, request_sent=False).state == "failed_retryable"
    assert classify_send_failure(RuntimeError("bug"), request_sent=True).state == "uncertain"
    unknown = ProviderError("dropped", retryable=True, delivery_unknown=True)
    assert classify_send_failure(unknown, request_sent=True).state == "uncertain"
    assert classify_send_failure(PublishRefused("bad", "no"), request_sent=True).state == (
        "failed_final"
    )
    assert classify_send_failure(PublishRetryLater("q", "later"), request_sent=True).state == (
        "failed_retryable"
    )
    assert classify_send_failure(PublishAuthRevoked(), request_sent=False).state == "failed_final"


# -------------------------------------------------------------- adapters


def _context(post_type: str, platform: Platform, settings: dict[str, Any]) -> PublishContext:
    return PublishContext(
        workspace_id=1,
        post_id=1,
        target_id=7,
        platform=platform,
        post_type=post_type,
        publisher="assisted",
        account=None,
        title="Title",
        caption="Caption [mock:uncertain]",
        settings=settings,
        media=(),
        planned_at=datetime(2026, 12, 1, 15, 0, tzinfo=UTC),
        payload_hash="f" * 64,
        ai_generated=False,
    )


def test_assisted_checklist_states_every_choice_and_sends_nothing(tmp_path: Path) -> None:
    publisher = AssistedPublisher(Database(Settings(data_dir=tmp_path / "data")))
    ctx = _context(
        "youtube_video",
        Platform.youtube,
        {
            "title": "My video",
            "privacy": "unlisted",
            "made_for_kids": False,
            "has_paid_product_placement": True,
            "contains_synthetic_media": False,
        },
    )
    assert publisher.preflight(ctx) == []
    result = publisher.send(ctx, NullProgress())
    assert result.status == "needs_user"
    text = "\n".join(result.checklist)
    assert "Unlisted" in text
    assert "Made for kids: No" in text
    assert "Paid product placement: Yes" in text
    assert "mark it as published" in text


def test_mock_scenarios_are_derived_from_the_caption_marker(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    youtube = next(p for p in mock_publishers(settings) if p.name == "youtube_direct")
    assert isinstance(youtube, MockPublisher)
    ctx = _context("youtube_video", Platform.youtube, {})
    marked: list[datetime] = []
    ctx._mark_request_sent = lambda: marked.append(datetime.now(UTC)) or marked[0]
    with pytest.raises(httpx.ReadTimeout):
        youtube.send(ctx, NullProgress())
    assert marked, "the mock marks the point of no return before the creating call"
    assert youtube.reconcile(ctx).status == "published"


# ---------------------------------------------------- model runtimes cannot publish

PUBLISHING_MODULES = (
    "runway.posts",
    "runway.platforms.publisher",
    "runway.platforms.publisher_registry",
    "runway.platforms.assisted_publisher",
    "runway.platforms.mock_publishers",
)
MODEL_PACKAGES = ("intelligence", "generation", "captions", "evaluation", "ranking")


def test_model_runtime_packages_never_import_the_publishing_engine() -> None:
    offenders: list[str] = []
    for package in MODEL_PACKAGES:
        for path in (ROOT / "src" / "runway" / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.startswith(PUBLISHING_MODULES):
                        offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert offenders == []
