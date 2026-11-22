"""Declared machines for draft, target, comment and comment-reply action status."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from runway.db.base import Database
from runway.db.models import ContentDraft, DraftTarget
from runway.domain.enums import DraftStatus, PlatformActionState, TargetState
from runway.domain.state_machine import (
    COMMENT_STATUS_MACHINE,
    DRAFT_STATUS_MACHINE,
    PLATFORM_ACTION_MACHINE,
    TARGET_STATE_MACHINE,
    InvalidStateTransition,
    StateMachine,
    UnknownState,
    derive_draft_status,
)
from runway.platforms.drafts import DraftService, InvalidDraftUpdate

MACHINES: list[StateMachine] = [  # type: ignore[type-arg]
    DRAFT_STATUS_MACHINE,
    TARGET_STATE_MACHINE,
    PLATFORM_ACTION_MACHINE,
    COMMENT_STATUS_MACHINE,
]


@pytest.mark.parametrize("machine", MACHINES, ids=lambda machine: machine.name)
def test_every_state_is_declared_and_edges_stay_inside_the_vocabulary(machine) -> None:
    assert set(machine.transitions) == set(machine.states)
    for targets in machine.transitions.values():
        assert targets <= set(machine.states)
    assert machine.initial <= set(machine.states)


def test_stored_vocabularies_are_unchanged() -> None:
    assert {state.value for state in DraftStatus} == {
        "draft",
        "scheduled",
        "partially_published",
        "published",
        "archived",
    }
    assert {state.value for state in PlatformActionState} == {
        "proposed",
        "attempted",
        "completed",
        "failed",
        "uncertain",
    }
    # Spec 03 names; the unsent target keeps its legacy stored value.
    assert TargetState.DRAFT.value == "pending"
    assert TargetState.PUBLISHED.value == "published"
    assert {state.name.lower() for state in TargetState} == {
        "draft",
        "ready",
        "released",
        "queued",
        "sending",
        "processing",
        "published",
        "verified",
        "needs_user",
        "failed_retryable",
        "failed_final",
        "uncertain",
        "cancelled",
    }


def test_target_machine_allows_only_todays_manual_path() -> None:
    assert TARGET_STATE_MACHINE.allowed("pending", "published")
    with pytest.raises(InvalidStateTransition):
        TARGET_STATE_MACHINE.require("published", "pending")
    # Engine states are declared but cannot be entered until the publishing job adds edges.
    for state in TargetState:
        if state not in {TargetState.DRAFT, TargetState.PUBLISHED}:
            assert not TARGET_STATE_MACHINE.allowed(TargetState.DRAFT, state)
    with pytest.raises(UnknownState):
        TARGET_STATE_MACHINE.parse("sent")


def test_draft_machine_rejects_contradictory_moves() -> None:
    with pytest.raises(InvalidStateTransition):
        DRAFT_STATUS_MACHINE.require("published", "draft")
    with pytest.raises(InvalidStateTransition):
        DRAFT_STATUS_MACHINE.require("archived", "published")
    with pytest.raises(InvalidStateTransition):
        DRAFT_STATUS_MACHINE.require("partially_published", "scheduled")
    assert DRAFT_STATUS_MACHINE.allowed("archived", "draft")
    # Writing the current value again is not a transition.
    assert DRAFT_STATUS_MACHINE.allowed("archived", "archived")


def test_comment_reply_action_never_leaves_a_settled_state() -> None:
    action = type("Action", (), {"state": "proposed"})()
    PLATFORM_ACTION_MACHINE.apply(action, PlatformActionState.ATTEMPTED)
    PLATFORM_ACTION_MACHINE.apply(action, PlatformActionState.UNCERTAIN)
    with pytest.raises(InvalidStateTransition):
        PLATFORM_ACTION_MACHINE.apply(action, PlatformActionState.ATTEMPTED)
    assert action.state == "uncertain"
    with pytest.raises(InvalidStateTransition):
        PLATFORM_ACTION_MACHINE.require("completed", "failed")
    with pytest.raises(InvalidStateTransition):
        PLATFORM_ACTION_MACHINE.start(PlatformActionState.COMPLETED)


def test_comment_labels_may_follow_one_another() -> None:
    assert COMMENT_STATUS_MACHINE.allowed("answered", "new")
    with pytest.raises(UnknownState):
        COMMENT_STATUS_MACHINE.parse("spam")


@pytest.mark.parametrize(
    ("current", "scheduled", "targets", "expected"),
    [
        ("draft", True, ["pending"], "scheduled"),
        ("scheduled", False, ["pending"], "draft"),
        ("scheduled", True, ["published", "pending"], "partially_published"),
        ("partially_published", True, ["published", "published"], "published"),
        ("archived", True, ["published"], "archived"),
        ("published", False, [], "published"),
        ("draft", False, [], "draft"),
    ],
)
def test_derived_draft_status(current, scheduled, targets, expected) -> None:
    assert derive_draft_status(current, scheduled=scheduled, target_states=targets) == expected


# ------------------------------------------------------------ service behaviour


def _draft_with_target(service: DraftService) -> tuple[int, int]:
    view = service.create_draft(
        title="Cross-post",
        scheduled_for=datetime.now(UTC) + timedelta(days=1),
        targets=[{"platform": "youtube"}],
    )
    targets = view["targets"]
    assert isinstance(targets, list)
    return int(view["id"]), int(targets[0]["id"])


def test_clearing_the_schedule_returns_the_draft_to_draft(database: Database, settings) -> None:
    service = DraftService(database, settings)
    draft_id, _ = _draft_with_target(service)
    assert service.get_draft(draft_id)["status"] == "scheduled"  # type: ignore[index]

    view = service.update_draft(draft_id, clear_schedule=True)

    assert view["status"] == "draft"
    assert view["scheduled_for"] is None


def test_explicit_status_must_agree_with_the_schedule(database: Database, settings) -> None:
    service = DraftService(database, settings)
    draft_id, _ = _draft_with_target(service)
    with pytest.raises(InvalidDraftUpdate):
        service.update_draft(draft_id, status="draft")
    service.update_draft(draft_id, clear_schedule=True)
    with pytest.raises(InvalidDraftUpdate):
        service.update_draft(draft_id, status="scheduled")


def test_a_published_draft_cannot_be_set_back_to_draft(database: Database, settings) -> None:
    service = DraftService(database, settings)
    draft_id, target_id = _draft_with_target(service)
    service.update_draft(draft_id, clear_schedule=True)
    published = service.mark_published(draft_id, target_id, "https://youtu.be/abc123XYZ_-")
    assert published["status"] == "published"

    with pytest.raises(InvalidStateTransition):
        service.update_draft(draft_id, status="draft")


def test_publishing_a_target_of_an_archived_draft_keeps_it_archived(
    database: Database, settings
) -> None:
    service = DraftService(database, settings)
    draft_id, target_id = _draft_with_target(service)
    service.archive_draft(draft_id)

    view = service.mark_published(draft_id, target_id, "https://youtu.be/abc123XYZ_-")

    # Before the machine, _recompute_status silently un-archived it.
    assert view["status"] == "archived"
    targets = view["targets"]
    assert isinstance(targets, list) and targets[0]["status"] == "published"


def test_a_target_with_an_unknown_stored_status_is_not_overwritten(
    database: Database, settings
) -> None:
    service = DraftService(database, settings)
    draft_id, target_id = _draft_with_target(service)
    with database.session() as session:
        target = session.get(DraftTarget, target_id)
        assert target is not None
        target.status = "sent_by_hand"  # a value no code writes

    with pytest.raises(UnknownState):
        service.mark_published(draft_id, target_id, "https://youtu.be/abc123XYZ_-")
    with database.session() as session:
        assert session.scalar(select(DraftTarget.status).where(DraftTarget.id == target_id)) == (
            "sent_by_hand"
        )
        assert session.get(ContentDraft, draft_id) is not None


# -------------------------------------------------------- no free-string writes

MACHINE_MODELS = {"ContentDraft", "DraftTarget", "CommentRecord", "PlatformAction"}
MACHINE_WRITERS = ("platforms/drafts.py", "platforms/inbox.py")


def test_machine_columns_are_never_written_as_string_literals() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "runway"
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(source_root).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in MACHINE_MODELS
            ):
                for keyword in node.keywords:
                    if keyword.arg in {"status", "state"} and isinstance(
                        keyword.value, ast.Constant
                    ):
                        offenders.append(f"{relative}:{node.lineno} {node.func.id}({keyword.arg}=)")
            if relative in MACHINE_WRITERS and isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr in {"status", "state"}
                        and isinstance(node.value, ast.Constant | ast.IfExp | ast.Name)
                    ):
                        offenders.append(f"{relative}:{node.lineno} .{target.attr} =")
    assert offenders == []


ORDER = {"draft": 0, "scheduled": 0, "partially_published": 1, "published": 2, "archived": 3}


@pytest.mark.parametrize("current", ["draft", "scheduled", "partially_published", "published"])
@pytest.mark.parametrize("scheduled", [True, False])
@pytest.mark.parametrize(
    "targets",
    [[], ["pending"], ["published"], ["published", "pending"], ["published", "published"]],
)
def test_derived_draft_status_never_moves_backwards(current, scheduled, targets) -> None:
    derived = derive_draft_status(current, scheduled=scheduled, target_states=targets)
    assert ORDER[derived.value] >= ORDER[current]
    # Every derived status is a legal transition from the current one.
    assert DRAFT_STATUS_MACHINE.allowed(current, derived)


def test_a_published_draft_that_gains_an_unsent_target_stays_published() -> None:
    assert (
        derive_draft_status("published", scheduled=True, target_states=["published", "pending"])
        == "published"
    )
