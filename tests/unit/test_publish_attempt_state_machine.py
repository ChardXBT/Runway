from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from runway.db.models import PublishAttempt
from runway.domain.enums import PublishAttemptStatus
from runway.domain.state_machine import (
    ATTEMPT_ACTIVE_STATUSES,
    ATTEMPT_PENDING_STATUSES,
    ATTEMPT_PRE_SUBMISSION_STATUSES,
    ATTEMPT_TRANSITIONS,
    ATTEMPT_UNFINISHED_STATUSES,
    InvalidAttemptTransition,
    attempt_status_of,
    attempt_transition_allowed,
    require_attempt_transition,
    transition_attempt,
)

_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(minutes=5)


def _attempt(
    status: str,
    *,
    submitted_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> PublishAttempt:
    return PublishAttempt(
        proposal_id=1,
        publisher="youtube",
        status=status,
        confirmation_token_hash="a" * 64,
        payload_hash="b" * 64,
        planned_publish_at="2026-11-09T10:00:00-05:00",
        expires_at=_LATER,
        submitted_at=submitted_at,
        completed_at=completed_at,
    )


def test_declared_vocabulary_matches_the_legacy_status_strings() -> None:
    assert {state.value for state in PublishAttemptStatus} == {
        "prepared",
        "queued",
        "submitting",
        "blocked_session",
        "verified",
        "submitted_unverified",
        "failed_before_submission",
        "expired",
        "invalidated",
        "superseded",
        "unknown",
    }


def test_transition_table_covers_the_documented_lifecycle() -> None:
    assert attempt_transition_allowed(
        PublishAttemptStatus.PREPARED, PublishAttemptStatus.SUBMITTING
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.PREPARED, PublishAttemptStatus.SUPERSEDED
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.PREPARED, PublishAttemptStatus.EXPIRED
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.PREPARED, PublishAttemptStatus.INVALIDATED
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.QUEUED, PublishAttemptStatus.SUBMITTING
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.QUEUED, PublishAttemptStatus.BLOCKED_SESSION
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.QUEUED, PublishAttemptStatus.INVALIDATED
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.QUEUED, PublishAttemptStatus.SUPERSEDED
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.SUBMITTING, PublishAttemptStatus.VERIFIED
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.SUBMITTING,
        PublishAttemptStatus.SUBMITTED_UNVERIFIED,
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.SUBMITTING,
        PublishAttemptStatus.FAILED_BEFORE_SUBMISSION,
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.BLOCKED_SESSION, PublishAttemptStatus.QUEUED
    )
    assert attempt_transition_allowed(
        PublishAttemptStatus.BLOCKED_SESSION, PublishAttemptStatus.SUPERSEDED
    )


def test_every_settled_state_rejects_every_further_transition() -> None:
    settled = {
        PublishAttemptStatus.VERIFIED,
        PublishAttemptStatus.SUBMITTED_UNVERIFIED,
        PublishAttemptStatus.FAILED_BEFORE_SUBMISSION,
        PublishAttemptStatus.EXPIRED,
        PublishAttemptStatus.INVALIDATED,
        PublishAttemptStatus.SUPERSEDED,
        PublishAttemptStatus.UNKNOWN,
    }
    for old in settled:
        for new in PublishAttemptStatus:
            with pytest.raises(InvalidAttemptTransition):
                require_attempt_transition(old, new)


def test_invalid_but_representable_transitions_raise() -> None:
    for old, new in (
        (PublishAttemptStatus.PREPARED, PublishAttemptStatus.VERIFIED),
        (PublishAttemptStatus.PREPARED, PublishAttemptStatus.QUEUED),
        (PublishAttemptStatus.QUEUED, PublishAttemptStatus.VERIFIED),
        (PublishAttemptStatus.QUEUED, PublishAttemptStatus.PREPARED),
        (PublishAttemptStatus.SUBMITTING, PublishAttemptStatus.QUEUED),
        (PublishAttemptStatus.SUBMITTING, PublishAttemptStatus.SUPERSEDED),
        (PublishAttemptStatus.BLOCKED_SESSION, PublishAttemptStatus.SUBMITTING),
        (PublishAttemptStatus.SUBMITTED_UNVERIFIED, PublishAttemptStatus.QUEUED),
    ):
        assert not attempt_transition_allowed(old, new)
        with pytest.raises(InvalidAttemptTransition):
            require_attempt_transition(old, new)


def test_transition_attempt_writes_the_derived_timestamps() -> None:
    attempt = _attempt("queued")
    transition_attempt(attempt, PublishAttemptStatus.SUBMITTING, now=_NOW)
    assert attempt.status == "submitting"
    assert attempt.submitted_at == _NOW
    assert attempt.completed_at is None

    transition_attempt(attempt, PublishAttemptStatus.VERIFIED, now=_LATER)
    assert attempt.status == "verified"
    assert attempt.submitted_at == _NOW
    assert attempt.completed_at == _LATER


def test_transition_attempt_requeue_clears_the_settled_marker() -> None:
    attempt = _attempt("blocked_session", completed_at=_NOW)
    transition_attempt(attempt, PublishAttemptStatus.QUEUED, now=_LATER)
    assert attempt.status == "queued"
    assert attempt.submitted_at is None
    assert attempt.completed_at is None


def test_transition_attempt_rejects_rows_with_corrupt_state() -> None:
    corrupt = _attempt("queued", completed_at=_NOW)
    with pytest.raises(InvalidAttemptTransition):
        transition_attempt(corrupt, PublishAttemptStatus.SUBMITTING, now=_LATER)
    assert corrupt.status == "queued"

    with pytest.raises(InvalidAttemptTransition):
        transition_attempt(
            _attempt("verified", submitted_at=_NOW, completed_at=_NOW),
            PublishAttemptStatus.QUEUED,
            now=_LATER,
        )


def test_transition_attempt_rejects_an_unknown_target() -> None:
    attempt = _attempt("queued")
    with pytest.raises(ValueError):
        transition_attempt(attempt, "made_up_state", now=_NOW)
    assert attempt.status == "queued"


def test_attempt_status_of_resolves_every_legacy_row_encoding() -> None:
    legacy_rows = {
        "prepared": _attempt("prepared"),
        "queued": _attempt("queued"),
        "submitting": _attempt("submitting", submitted_at=_NOW),
        "blocked_session": _attempt("blocked_session", completed_at=_NOW),
        "verified": _attempt("verified", submitted_at=_NOW, completed_at=_LATER),
        "submitted_unverified": _attempt(
            "submitted_unverified", submitted_at=_NOW, completed_at=_LATER
        ),
        "failed_before_submission": _attempt(
            "failed_before_submission", submitted_at=_NOW, completed_at=_LATER
        ),
        "expired": _attempt("expired", completed_at=_NOW),
        "invalidated": _attempt("invalidated", completed_at=_NOW),
        "superseded": _attempt("superseded", completed_at=_NOW),
    }
    for raw_status, attempt in legacy_rows.items():
        assert attempt_status_of(attempt) is PublishAttemptStatus(raw_status)


def test_attempt_status_of_maps_unrepresentable_combos_to_unknown() -> None:
    assert attempt_status_of(_attempt("submitting")) is PublishAttemptStatus.UNKNOWN
    assert (
        attempt_status_of(_attempt("verified", submitted_at=_NOW))
        is PublishAttemptStatus.UNKNOWN
    )
    assert (
        attempt_status_of(_attempt("expired")) is PublishAttemptStatus.UNKNOWN
    )
    assert (
        attempt_status_of(_attempt("queued", completed_at=_NOW))
        is PublishAttemptStatus.UNKNOWN
    )
    assert (
        attempt_status_of(_attempt("queued", submitted_at=_NOW))
        is PublishAttemptStatus.UNKNOWN
    )
    assert (
        attempt_status_of(_attempt("blocked_session", submitted_at=_NOW))
        is PublishAttemptStatus.UNKNOWN
    )
    assert (
        attempt_status_of(_attempt("made_up_state")) is PublishAttemptStatus.UNKNOWN
    )


def test_read_sets_keep_the_queue_coordinator_semantics() -> None:
    assert frozenset(
        {
            PublishAttemptStatus.QUEUED,
            PublishAttemptStatus.SUBMITTING,
            PublishAttemptStatus.BLOCKED_SESSION,
        }
    ) == ATTEMPT_ACTIVE_STATUSES
    assert frozenset(
        {PublishAttemptStatus.QUEUED, PublishAttemptStatus.SUBMITTING}
    ) == ATTEMPT_PENDING_STATUSES
    assert frozenset(
        {
            PublishAttemptStatus.PREPARED,
            PublishAttemptStatus.QUEUED,
            PublishAttemptStatus.BLOCKED_SESSION,
        }
    ) == ATTEMPT_PRE_SUBMISSION_STATUSES
    assert frozenset(
        {
            PublishAttemptStatus.PREPARED,
            PublishAttemptStatus.QUEUED,
            PublishAttemptStatus.SUBMITTING,
            PublishAttemptStatus.BLOCKED_SESSION,
        }
    ) == ATTEMPT_UNFINISHED_STATUSES
    assert PublishAttemptStatus.UNKNOWN not in ATTEMPT_ACTIVE_STATUSES
    assert PublishAttemptStatus.UNKNOWN not in ATTEMPT_PENDING_STATUSES
    assert PublishAttemptStatus.UNKNOWN not in ATTEMPT_PRE_SUBMISSION_STATUSES
    assert PublishAttemptStatus.UNKNOWN not in ATTEMPT_UNFINISHED_STATUSES


def test_every_non_unknown_state_is_reachable_from_a_documented_path() -> None:
    reachable = {PublishAttemptStatus.PREPARED, PublishAttemptStatus.QUEUED}
    frontier = set(reachable)
    while frontier:
        state = frontier.pop()
        for target in ATTEMPT_TRANSITIONS[state]:
            if target not in reachable:
                reachable.add(target)
                frontier.add(target)
    assert reachable == set(PublishAttemptStatus) - {PublishAttemptStatus.UNKNOWN}
