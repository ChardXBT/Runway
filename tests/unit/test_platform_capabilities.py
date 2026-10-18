from __future__ import annotations

from runway.platforms.capabilities import (
    YOUTUBE_ENGAGE_SCOPE,
    YOUTUBE_READONLY_SCOPE,
    Capability,
    Platform,
    capability_matrix,
    capability_ready,
)

ALL_CAPABILITIES = {capability.value for capability in Capability}


def test_every_platform_exposes_all_nine_capabilities() -> None:
    for platform in Platform:
        matrix = capability_matrix(platform)
        assert {entry.capability.value for entry in matrix} == ALL_CAPABILITIES
        assert len(matrix) == 9


def test_youtube_reply_capability_requires_engage_scope() -> None:
    matrix = capability_matrix(Platform.youtube, engage_scopes=True, replies_enabled=True)
    entry = next(entry for entry in matrix if entry.capability is Capability.comments_reply)
    assert entry.state == "implemented"
    assert entry.requires_scopes == [YOUTUBE_ENGAGE_SCOPE]


def test_ready_is_false_when_account_disconnected() -> None:
    entry = next(
        entry
        for entry in capability_matrix(Platform.youtube)
        if entry.capability is Capability.content_inventory
    )
    assert (
        capability_ready(
            entry,
            account_status="disconnected",
            granted_scopes=[YOUTUBE_READONLY_SCOPE],
        )
        is False
    )


def test_ready_is_false_when_scope_missing() -> None:
    entry = next(
        entry
        for entry in capability_matrix(Platform.youtube)
        if entry.capability is Capability.content_inventory
    )
    assert capability_ready(entry, account_status="connected", granted_scopes=[]) is False


def test_ready_is_false_for_non_implemented_states() -> None:
    matrix = capability_matrix(Platform.tiktok)
    for entry in matrix:
        if entry.state == "implemented":
            continue
        assert (
            capability_ready(entry, account_status="connected", granted_scopes=["anything"])
            is False
        )


def test_ready_is_true_only_when_state_account_and_scopes_hold() -> None:
    entry = next(
        entry
        for entry in capability_matrix(Platform.instagram)
        if entry.capability is Capability.content_inventory
    )
    assert (
        capability_ready(
            entry,
            account_status="connected",
            granted_scopes=["instagram_business_basic", "extra_scope"],
        )
        is True
    )
