from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from runway.domain.enums import ProposalStatus, PublishAttemptStatus

ALLOWED_TRANSITIONS: dict[ProposalStatus, set[ProposalStatus]] = {
    ProposalStatus.GENERATING: {ProposalStatus.NEEDS_REVIEW, ProposalStatus.CANCELLED},
    ProposalStatus.NEEDS_REVIEW: {
        ProposalStatus.APPROVED,
        ProposalStatus.REJECTED,
        ProposalStatus.CANCELLED,
    },
    ProposalStatus.APPROVED: {
        ProposalStatus.NEEDS_REVIEW,
        ProposalStatus.INTERNALLY_SCHEDULED,
        ProposalStatus.CANCELLED,
    },
    ProposalStatus.REJECTED: {ProposalStatus.NEEDS_REVIEW, ProposalStatus.CANCELLED},
    ProposalStatus.INTERNALLY_SCHEDULED: {
        ProposalStatus.PUBLISHING,
        ProposalStatus.CANCELLED,
    },
    ProposalStatus.PUBLISHING: {
        ProposalStatus.EXTERNALLY_SCHEDULED,
        ProposalStatus.PUBLISH_UNVERIFIED,
        ProposalStatus.PUBLISH_FAILED,
    },
    ProposalStatus.EXTERNALLY_SCHEDULED: {
        ProposalStatus.PUBLISHED,
        ProposalStatus.CANCELLED,
    },
    ProposalStatus.PUBLISH_FAILED: {
        ProposalStatus.PUBLISHING,
        ProposalStatus.CANCELLED,
    },
    ProposalStatus.PUBLISH_UNVERIFIED: {
        ProposalStatus.EXTERNALLY_SCHEDULED,
        ProposalStatus.CANCELLED,
    },
    ProposalStatus.PUBLISHED: set(),
    ProposalStatus.CANCELLED: set(),
}


@dataclass(slots=True)
class InvalidTransition(ValueError):
    old: ProposalStatus
    new: ProposalStatus

    def __str__(self) -> str:
        return f"proposal cannot transition from {self.old.value} to {self.new.value}"


def transition_allowed(old: ProposalStatus | str, new: ProposalStatus | str) -> bool:
    old_status = ProposalStatus(old)
    new_status = ProposalStatus(new)
    return new_status in ALLOWED_TRANSITIONS[old_status]


def require_transition(old: ProposalStatus | str, new: ProposalStatus | str) -> None:
    old_status = ProposalStatus(old)
    new_status = ProposalStatus(new)
    if not transition_allowed(old_status, new_status):
        raise InvalidTransition(old_status, new_status)


# ---------------------------------------------------------------------------
# PublishAttempt lifecycle
#
# Persisted on `PublishAttempt.status` as the same strings the enum members
# carry. The two nullable timestamps are derived signals, not independent
# state: `submitted_at` records that the attempt crossed the submission
# boundary and `completed_at` records that the attempt reached a settled
# phase. Rows whose columns disagree with those derivations resolve to
# UNKNOWN instead of a lifecycle state.
# ---------------------------------------------------------------------------

ATTEMPT_REACHED_SUBMISSION_STATUSES: frozenset[PublishAttemptStatus] = frozenset(
    {
        PublishAttemptStatus.SUBMITTING,
        PublishAttemptStatus.VERIFIED,
        PublishAttemptStatus.SUBMITTED_UNVERIFIED,
        PublishAttemptStatus.FAILED_BEFORE_SUBMISSION,
    }
)

ATTEMPT_COMPLETED_STATUSES: frozenset[PublishAttemptStatus] = frozenset(
    {
        PublishAttemptStatus.VERIFIED,
        PublishAttemptStatus.SUBMITTED_UNVERIFIED,
        PublishAttemptStatus.FAILED_BEFORE_SUBMISSION,
        PublishAttemptStatus.EXPIRED,
        PublishAttemptStatus.INVALIDATED,
        PublishAttemptStatus.SUPERSEDED,
        PublishAttemptStatus.BLOCKED_SESSION,
    }
)

# Attempts that have not crossed the submission boundary; a Lineup change may
# supersede them without any risk of double-submitting to YouTube.
ATTEMPT_PRE_SUBMISSION_STATUSES: frozenset[PublishAttemptStatus] = frozenset(
    {
        PublishAttemptStatus.PREPARED,
        PublishAttemptStatus.QUEUED,
        PublishAttemptStatus.BLOCKED_SESSION,
    }
)

# Attempts the queue coordinator still tracks as work (queued + blocked is
# persisted work, submitting is an in-flight submission).
ATTEMPT_ACTIVE_STATUSES: frozenset[PublishAttemptStatus] = frozenset(
    {
        PublishAttemptStatus.QUEUED,
        PublishAttemptStatus.SUBMITTING,
        PublishAttemptStatus.BLOCKED_SESSION,
    }
)

# Attempts occupying a proposal's submission slot; a new queue request for the
# same proposal dedupes onto these instead of double-scheduling.
ATTEMPT_PENDING_STATUSES: frozenset[PublishAttemptStatus] = frozenset(
    {
        PublishAttemptStatus.QUEUED,
        PublishAttemptStatus.SUBMITTING,
    }
)

# Any attempt whose publisher work is not settled; used by guards that must
# not run while a submission could still be in flight.
ATTEMPT_UNFINISHED_STATUSES: frozenset[PublishAttemptStatus] = frozenset(
    {
        PublishAttemptStatus.PREPARED,
        PublishAttemptStatus.QUEUED,
        PublishAttemptStatus.SUBMITTING,
        PublishAttemptStatus.BLOCKED_SESSION,
    }
)

ATTEMPT_TRANSITIONS: dict[PublishAttemptStatus, set[PublishAttemptStatus]] = {
    PublishAttemptStatus.PREPARED: {
        PublishAttemptStatus.SUBMITTING,
        PublishAttemptStatus.SUPERSEDED,
        PublishAttemptStatus.EXPIRED,
        PublishAttemptStatus.INVALIDATED,
    },
    PublishAttemptStatus.QUEUED: {
        PublishAttemptStatus.SUBMITTING,
        PublishAttemptStatus.BLOCKED_SESSION,
        PublishAttemptStatus.INVALIDATED,
        PublishAttemptStatus.SUPERSEDED,
    },
    PublishAttemptStatus.SUBMITTING: {
        PublishAttemptStatus.VERIFIED,
        PublishAttemptStatus.SUBMITTED_UNVERIFIED,
        PublishAttemptStatus.FAILED_BEFORE_SUBMISSION,
    },
    PublishAttemptStatus.BLOCKED_SESSION: {
        PublishAttemptStatus.QUEUED,
        PublishAttemptStatus.SUPERSEDED,
    },
    PublishAttemptStatus.VERIFIED: set(),
    PublishAttemptStatus.SUBMITTED_UNVERIFIED: set(),
    PublishAttemptStatus.FAILED_BEFORE_SUBMISSION: set(),
    PublishAttemptStatus.EXPIRED: set(),
    PublishAttemptStatus.INVALIDATED: set(),
    PublishAttemptStatus.SUPERSEDED: set(),
    PublishAttemptStatus.UNKNOWN: set(),
}


@dataclass(slots=True)
class InvalidAttemptTransition(ValueError):
    old: PublishAttemptStatus
    new: PublishAttemptStatus

    def __str__(self) -> str:
        return (
            f"publish attempt cannot transition from {self.old.value} "
            f"to {self.new.value}"
        )


def attempt_transition_allowed(
    old: PublishAttemptStatus | str,
    new: PublishAttemptStatus | str,
) -> bool:
    old_status = PublishAttemptStatus(old)
    new_status = PublishAttemptStatus(new)
    return new_status in ATTEMPT_TRANSITIONS[old_status]


def require_attempt_transition(
    old: PublishAttemptStatus | str,
    new: PublishAttemptStatus | str,
) -> None:
    old_status = PublishAttemptStatus(old)
    new_status = PublishAttemptStatus(new)
    if not attempt_transition_allowed(old_status, new_status):
        raise InvalidAttemptTransition(old_status, new_status)


class AttemptStateFields(Protocol):
    """The persisted fields the attempt lifecycle reads and derives."""

    status: str
    submitted_at: datetime | None
    completed_at: datetime | None


def attempt_row_status(
    status: PublishAttemptStatus | str | None,
    *,
    submitted_at: datetime | None,
    completed_at: datetime | None,
) -> PublishAttemptStatus:
    """Resolve a persisted row to its declared state.

    Unknown strings and status/timestamp combos the machine cannot produce
    resolve to UNKNOWN, which is never active, pending, or transitionable.
    """
    try:
        state = PublishAttemptStatus(status)  # type: ignore[arg-type]
    except ValueError:
        return PublishAttemptStatus.UNKNOWN
    if state is PublishAttemptStatus.UNKNOWN:
        return state
    if (submitted_at is not None) != (state in ATTEMPT_REACHED_SUBMISSION_STATUSES):
        return PublishAttemptStatus.UNKNOWN
    if (completed_at is not None) != (state in ATTEMPT_COMPLETED_STATUSES):
        return PublishAttemptStatus.UNKNOWN
    return state


def attempt_status_of(attempt: AttemptStateFields) -> PublishAttemptStatus:
    return attempt_row_status(
        attempt.status,
        submitted_at=attempt.submitted_at,
        completed_at=attempt.completed_at,
    )


def transition_attempt(
    attempt: AttemptStateFields,
    new: PublishAttemptStatus | str,
    *,
    now: datetime,
) -> PublishAttemptStatus:
    """Apply a validated from→to transition plus the derived timestamps.

    `submitted_at` is written exactly when the attempt crosses the submission
    boundary; `completed_at` is written when it reaches a settled phase and
    cleared when it re-enters pending work (a blocked attempt requeued).
    """
    old = attempt_status_of(attempt)
    new_status = PublishAttemptStatus(new)
    require_attempt_transition(old, new_status)
    attempt.status = new_status.value
    if new_status is PublishAttemptStatus.SUBMITTING:
        attempt.submitted_at = now
    attempt.completed_at = now if new_status in ATTEMPT_COMPLETED_STATUSES else None
    return new_status
