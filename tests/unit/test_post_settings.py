"""Target settings schemas, the release payload hash, the target machine and post status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from runway.domain.enums import TargetState
from runway.domain.state_machine import (
    TARGET_RELEASABLE_STATES,
    TARGET_STATE_MACHINE,
    InvalidStateTransition,
)
from runway.platforms.post_settings import (
    InvalidTargetSettings,
    explicit_privacy,
    parse_settings,
    release_notice,
    settings_issues,
)
from runway.posts.payload import canonical_payload, payload_hash
from runway.posts.states import derive_post_status
from runway.posts.types import TargetPostType

_T = TargetState

YOUTUBE_COMPLETE = {
    "title": "A title",
    "category_id": "22",
    "privacy": "unlisted",
    "made_for_kids": False,
    "has_paid_product_placement": False,
}


def _codes(post_type: TargetPostType, raw: dict[str, object], caption: str = "") -> set[str]:
    settings = parse_settings(post_type, raw)
    return {issue.code for issue in settings_issues(post_type, settings, caption=caption)}


def _fields(post_type: TargetPostType, raw: dict[str, object]) -> set[str | None]:
    settings = parse_settings(post_type, raw)
    return {
        issue.field
        for issue in settings_issues(post_type, settings, caption="")
        if issue.severity == "error"
    }


# ---------------------------------------------------------------- YouTube


@pytest.mark.parametrize("post_type", [TargetPostType.YOUTUBE_VIDEO, TargetPostType.YOUTUBE_SHORT])
def test_youtube_complete_settings_have_no_errors(post_type: TargetPostType) -> None:
    assert _fields(post_type, YOUTUBE_COMPLETE) == set()


@pytest.mark.parametrize(
    "missing", ["privacy", "made_for_kids", "has_paid_product_placement", "title", "category_id"]
)
def test_youtube_required_fields_have_no_default(missing: str) -> None:
    raw = {key: value for key, value in YOUTUBE_COMPLETE.items() if key != missing}
    assert missing in _fields(TargetPostType.YOUTUBE_VIDEO, raw)
    # The parsed model keeps the value unset; nothing fills it in for the creator.
    effective = parse_settings(TargetPostType.YOUTUBE_VIDEO, raw).effective(ai_generated=False)
    assert effective[missing] is None


def test_youtube_paid_placement_and_kids_flags_must_be_booleans() -> None:
    with pytest.raises(InvalidTargetSettings):
        parse_settings(TargetPostType.YOUTUBE_VIDEO, {"made_for_kids": "maybe"})
    with pytest.raises(InvalidTargetSettings):
        parse_settings(TargetPostType.YOUTUBE_VIDEO, {"has_paid_product_placement": "no idea"})
    # An explicit `true` is as valid as `false`.
    raw = {**YOUTUBE_COMPLETE, "has_paid_product_placement": True, "made_for_kids": True}
    assert _fields(TargetPostType.YOUTUBE_VIDEO, raw) == set()


def test_youtube_effective_defaults_only_fill_runway_owned_fields() -> None:
    effective = parse_settings(TargetPostType.YOUTUBE_VIDEO, YOUTUBE_COMPLETE).effective(
        ai_generated=True
    )
    assert effective["contains_synthetic_media"] is True  # from the post's ai_generated
    assert effective["notify_subscribers"] is True
    assert effective["schedule_mode"] == "native"
    assert effective["privacy"] == "unlisted"


def test_youtube_title_limits() -> None:
    raw = {**YOUTUBE_COMPLETE, "title": "x" * 101}
    assert "title_too_long" in _codes(TargetPostType.YOUTUBE_VIDEO, raw)
    raw = {**YOUTUBE_COMPLETE, "title": "<b>bold</b>"}
    assert "title_invalid" in _codes(TargetPostType.YOUTUBE_VIDEO, raw)


# -------------------------------------------------------------- privacy


@pytest.mark.parametrize(
    ("post_type", "raw", "explicit"),
    [
        (TargetPostType.YOUTUBE_VIDEO, {}, False),
        (TargetPostType.YOUTUBE_VIDEO, {"privacy": "private"}, True),
        (TargetPostType.TIKTOK_VIDEO, {}, False),
        (TargetPostType.TIKTOK_VIDEO, {"privacy_level": "SELF_ONLY"}, True),
        # Instagram has no privacy field: the post inherits the account's visibility.
        (TargetPostType.INSTAGRAM_REEL, {}, True),
        (TargetPostType.TIKTOK_DRAFT, {}, True),
    ],
)
def test_privacy_is_explicit_wherever_it_exists(
    post_type: TargetPostType, raw: dict[str, object], explicit: bool
) -> None:
    assert explicit_privacy(post_type, parse_settings(post_type, raw)) is explicit
    if not explicit:
        assert "privacy_required" in _codes(post_type, raw)


# ------------------------------------------------------------------ TikTok


def test_tiktok_mandatory_screen_starts_with_everything_off_and_no_privacy() -> None:
    settings = parse_settings(TargetPostType.TIKTOK_VIDEO, {})
    values = settings.model_dump()
    assert values["privacy_level"] is None
    assert values["allow_comment"] is values["allow_duet"] is values["allow_stitch"] is False
    assert values["commercial_content"] is False


def test_tiktok_commercial_content_rules() -> None:
    base = {"privacy_level": "PUBLIC_TO_EVERYONE", "commercial_content": True}
    assert "commercial_content_incomplete" in _codes(TargetPostType.TIKTOK_VIDEO, base)
    branded = {**base, "branded_content": True, "privacy_level": "SELF_ONLY"}
    assert "branded_content_private" in _codes(TargetPostType.TIKTOK_VIDEO, branded)
    ok = {**base, "your_brand": True}
    assert _fields(TargetPostType.TIKTOK_VIDEO, ok) == set()
    notice = release_notice(
        TargetPostType.TIKTOK_VIDEO,
        parse_settings(TargetPostType.TIKTOK_VIDEO, {**base, "branded_content": True}),
    )
    assert notice is not None and "Branded Content Policy" in notice


def test_tiktok_caption_counts_utf16_units() -> None:
    caption = "\U0001f600" * 1101  # 2,202 UTF-16 code units
    assert "caption_too_long" in _codes(
        TargetPostType.TIKTOK_VIDEO, {"privacy_level": "SELF_ONLY"}, caption
    )


def test_tiktok_draft_has_no_post_settings() -> None:
    with pytest.raises(InvalidTargetSettings):
        parse_settings(TargetPostType.TIKTOK_DRAFT, {"privacy_level": "SELF_ONLY"})


# --------------------------------------------------------------- Instagram


def test_instagram_field_rules_from_the_recheck() -> None:
    # No alt_text on Reels or Stories; alt_text only on images.
    with pytest.raises(InvalidTargetSettings):
        parse_settings(TargetPostType.INSTAGRAM_REEL, {"alt_text": "a dog"})
    with pytest.raises(InvalidTargetSettings):
        parse_settings(TargetPostType.INSTAGRAM_STORY, {"alt_text": "a dog"})
    parse_settings(TargetPostType.INSTAGRAM_IMAGE, {"alt_text": "a dog"})
    # is_ai_generated lives on the carousel parent; children carry no settings at all.
    parse_settings(TargetPostType.INSTAGRAM_CAROUSEL, {"is_ai_generated": True})
    with pytest.raises(InvalidTargetSettings):
        parse_settings(TargetPostType.INSTAGRAM_CAROUSEL, {"children": [{"is_ai_generated": 1}]})
    assert parse_settings(TargetPostType.INSTAGRAM_REEL, {"trial": "SS_PERFORMANCE"})
    with pytest.raises(InvalidTargetSettings):
        parse_settings(TargetPostType.INSTAGRAM_REEL, {"trial": "AUTO"})


def test_instagram_caption_limits() -> None:
    hashtags = " ".join(f"#tag{i}" for i in range(31))
    assert "too_many_hashtags" in _codes(TargetPostType.INSTAGRAM_IMAGE, {}, hashtags)
    assert "caption_too_long" in _codes(TargetPostType.INSTAGRAM_REEL, {}, "x" * 2201)
    collaborators = {"collaborators": ["a", "b", "c", "d"]}
    assert "too_many_collaborators" in _codes(TargetPostType.INSTAGRAM_IMAGE, collaborators)


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(InvalidTargetSettings) as raised:
        parse_settings(TargetPostType.YOUTUBE_VIDEO, {"privacyStatus": "public"})
    assert raised.value.errors[0]["loc"] == ["privacyStatus"]


# ---------------------------------------------------------------- payload hash

PAYLOAD = {
    "platform": "youtube",
    "post_type": "youtube_video",
    "account_external_id": "UC123",
    "caption": "Hello",
    "settings": {"privacy": "public", "made_for_kids": False},
    "media_sha256": ["a" * 64, "b" * 64],
    "planned_at": datetime(2026, 12, 1, 15, 0, tzinfo=UTC),
}


def test_payload_hash_is_sha256_of_canonical_json() -> None:
    first = payload_hash(canonical_payload(**PAYLOAD))  # type: ignore[arg-type]
    reordered = dict(PAYLOAD, settings={"made_for_kids": False, "privacy": "public"})
    assert payload_hash(canonical_payload(**reordered)) == first  # type: ignore[arg-type]
    assert len(first) == 64 and int(first, 16) >= 0
    # The same instant in another zone is the same planned time.
    toronto = timezone(timedelta(hours=-5))
    same = dict(PAYLOAD, planned_at=datetime(2026, 12, 1, 10, 0, tzinfo=toronto))
    assert payload_hash(canonical_payload(**same)) == first  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("caption", "Hello!"),
        ("settings", {"privacy": "private", "made_for_kids": False}),
        ("media_sha256", ["b" * 64, "a" * 64]),
        ("account_external_id", "UC999"),
        ("planned_at", datetime(2026, 12, 1, 15, 1, tzinfo=UTC)),
    ],
)
def test_every_bound_field_changes_the_hash(field: str, value: object) -> None:
    first = payload_hash(canonical_payload(**PAYLOAD))  # type: ignore[arg-type]
    changed = payload_hash(canonical_payload(**dict(PAYLOAD, **{field: value})))  # type: ignore[arg-type]
    assert changed != first


# ---------------------------------------------------------------- machine


def test_verified_is_reached_only_from_published() -> None:
    sources = {old for old in TargetState if TARGET_STATE_MACHINE.allowed(old, _T.VERIFIED)}
    assert sources == {_T.PUBLISHED, _T.VERIFIED}


def test_uncertain_is_never_retried_automatically() -> None:
    for state in (_T.QUEUED, _T.RELEASED, _T.SENDING, _T.READY):
        with pytest.raises(InvalidStateTransition):
            TARGET_STATE_MACHINE.require(_T.UNCERTAIN, state)
    assert _T.UNCERTAIN not in TARGET_RELEASABLE_STATES


def test_nothing_is_sent_without_passing_through_released_and_queued() -> None:
    entering_sending = {
        old
        for old in TargetState
        if old is not _T.SENDING and TARGET_STATE_MACHINE.allowed(old, _T.SENDING)
    }
    assert entering_sending == {_T.QUEUED}
    entering_queued = {
        old
        for old in TargetState
        if old is not _T.QUEUED and TARGET_STATE_MACHINE.allowed(old, _T.QUEUED)
    }
    # Released, an automatic retry, or a re-run after a crash before anything was sent.
    assert entering_queued == {_T.RELEASED, _T.FAILED_RETRYABLE, _T.SENDING}
    entering_released = {
        old
        for old in TargetState
        if old is not _T.RELEASED and TARGET_STATE_MACHINE.allowed(old, _T.RELEASED)
    }
    assert entering_released == TARGET_RELEASABLE_STATES


def test_a_released_target_can_return_to_ready_on_edit() -> None:
    assert TARGET_STATE_MACHINE.allowed(_T.RELEASED, _T.READY)
    assert TARGET_STATE_MACHINE.allowed(_T.QUEUED, _T.READY)


# -------------------------------------------------------------- post status


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], "draft"),
        ([_T.DRAFT, _T.READY], "draft"),
        ([_T.READY, _T.READY], "ready"),
        ([_T.QUEUED, _T.READY], "scheduled"),
        ([_T.SENDING, _T.QUEUED], "publishing"),
        ([_T.VERIFIED, _T.QUEUED], "partially_published"),
        ([_T.VERIFIED, _T.PUBLISHED, _T.CANCELLED], "published"),
        ([_T.UNCERTAIN, _T.VERIFIED], "needs_attention"),
        ([_T.CANCELLED], "cancelled"),
    ],
)
def test_post_status_is_derived_from_targets(states: list[TargetState], expected: str) -> None:
    assert derive_post_status(states).value == expected
