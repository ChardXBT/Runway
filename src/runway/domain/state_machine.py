from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from runway.domain.enums import (
    CommentStatus,
    DraftStatus,
    PlatformActionState,
    ProposalStatus,
    PublishAttemptStatus,
    TargetState,
)

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

# Attempts whose publisher work is provably over. Every other row, including
# one that resolves to UNKNOWN, must be treated as possibly unfinished work.
ATTEMPT_SETTLED_STATUSES: frozenset[PublishAttemptStatus] = ATTEMPT_COMPLETED_STATUSES - {
    PublishAttemptStatus.BLOCKED_SESSION
}

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
        return f"publish attempt cannot transition from {self.old.value} to {self.new.value}"


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


def attempt_blocks_new_work(attempt: AttemptStateFields) -> bool:
    """Fail-closed guard predicate: unfinished or UNKNOWN rows block new work.

    UNKNOWN is never executed, but its real phase is unknowable (it may be a
    corrupt live submission), so guards that must not double-schedule or run
    while a submission could be in flight treat it as unfinished.
    """
    state = attempt_status_of(attempt)
    return state is PublishAttemptStatus.UNKNOWN or state in ATTEMPT_UNFINISHED_STATUSES


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


# ---------------------------------------------------------------------------
# Creator-platform lifecycles: content drafts, draft targets, comment-reply
# actions and comment moderation labels.
#
# One generic machine per persisted column. Every write goes through
# `StateMachine.apply`, which validates the from->to edge against the declared
# table. Writing the current value again is not a transition and is allowed.
# ---------------------------------------------------------------------------

StateT = TypeVar("StateT", bound=StrEnum)


@dataclass(slots=True)
class InvalidStateTransition(ValueError):
    machine: str
    old: str
    new: str

    def __str__(self) -> str:
        return f"{self.machine} cannot transition from {self.old} to {self.new}"


@dataclass(slots=True)
class UnknownState(ValueError):
    machine: str
    value: object

    def __str__(self) -> str:
        return f"{self.machine} has no state {self.value!r}"


class StateMachine(Generic[StateT]):
    def __init__(
        self,
        name: str,
        states: type[StateT],
        transitions: Mapping[StateT, frozenset[StateT]],
        *,
        initial: frozenset[StateT],
        column: str = "status",
    ) -> None:
        missing = set(states) - set(transitions)
        if missing:
            raise ValueError(f"{name} machine has no transition entry for {sorted(missing)}")
        self.name = name
        self.states = states
        self.transitions: dict[StateT, frozenset[StateT]] = dict(transitions)
        self.initial = initial
        self.column = column

    def parse(self, value: object) -> StateT:
        if not isinstance(value, str):
            raise UnknownState(self.name, value)
        try:
            return self.states(value)
        except ValueError:
            raise UnknownState(self.name, value) from None

    def allowed(self, old: StateT | str, new: StateT | str) -> bool:
        old_state = self.parse(old)
        new_state = self.parse(new)
        return old_state == new_state or new_state in self.transitions[old_state]

    def require(self, old: StateT | str, new: StateT | str) -> StateT:
        old_state = self.parse(old)
        new_state = self.parse(new)
        if not self.allowed(old_state, new_state):
            raise InvalidStateTransition(self.name, old_state.value, new_state.value)
        return new_state

    def start(self, state: StateT) -> str:
        """The stored value for a newly created row."""
        if state not in self.initial:
            raise InvalidStateTransition(self.name, "(new)", state.value)
        return state.value

    def apply(self, record: object, new: StateT | str) -> StateT:
        """Validate and write the transition onto ``record``'s state column."""
        new_state = self.require(getattr(record, self.column), new)
        setattr(record, self.column, new_state.value)
        return new_state


_D = DraftStatus
DRAFT_STATUS_MACHINE: StateMachine[DraftStatus] = StateMachine(
    "content draft",
    DraftStatus,
    {
        _D.DRAFT: frozenset({_D.SCHEDULED, _D.PARTIALLY_PUBLISHED, _D.PUBLISHED, _D.ARCHIVED}),
        _D.SCHEDULED: frozenset({_D.DRAFT, _D.PARTIALLY_PUBLISHED, _D.PUBLISHED, _D.ARCHIVED}),
        _D.PARTIALLY_PUBLISHED: frozenset({_D.PUBLISHED, _D.ARCHIVED}),
        _D.PUBLISHED: frozenset({_D.ARCHIVED}),
        # Restoring an archived draft returns it to editing, never to published.
        _D.ARCHIVED: frozenset({_D.DRAFT, _D.SCHEDULED}),
    },
    initial=frozenset({_D.DRAFT, _D.SCHEDULED}),
)

_T = TargetState
# The spec 03 section 2 machine, on `DraftTarget.state`. Writes go through
# runway.posts.states.set_target_state, which also mirrors the legacy
# `status` column and records the `target.state_changed` event.
#
# - Editing returns an unsent target to draft or ready (release cleared).
# - `published` is reached by the worker, by a reconcile that found the post,
#   or by a person marking it published (assisted and manual workflow).
# - `verified` is reached only from `published`, by reading the post back
#   from the platform (invariant 3).
# - `uncertain` is left only by reconciliation or a person; never by a retry
#   (invariant 4): it has no edge to queued, released or sending.
# - A crash before the point of no return goes sending -> queued (re-run);
#   after it, sending -> uncertain.
TARGET_STATE_MACHINE: StateMachine[TargetState] = StateMachine(
    "draft target",
    TargetState,
    {
        _T.DRAFT: frozenset({_T.READY, _T.PUBLISHED, _T.CANCELLED}),
        _T.READY: frozenset({_T.DRAFT, _T.RELEASED, _T.PUBLISHED, _T.CANCELLED}),
        _T.RELEASED: frozenset({_T.DRAFT, _T.READY, _T.QUEUED, _T.PUBLISHED, _T.CANCELLED}),
        _T.QUEUED: frozenset({_T.DRAFT, _T.READY, _T.SENDING, _T.PUBLISHED, _T.CANCELLED}),
        _T.SENDING: frozenset(
            {
                _T.PROCESSING,
                _T.PUBLISHED,
                _T.NEEDS_USER,
                _T.FAILED_RETRYABLE,
                _T.FAILED_FINAL,
                _T.UNCERTAIN,
                # Nothing was sent: re-run after a crash before the marker, or
                # waiting for planned_at after an Instagram container was made.
                _T.QUEUED,
                # Refused by the engine before any call (payload changed,
                # missed while offline, rate limit).
                _T.READY,
            }
        ),
        _T.PROCESSING: frozenset(
            {
                _T.PUBLISHED,
                _T.NEEDS_USER,
                _T.FAILED_RETRYABLE,
                _T.FAILED_FINAL,
                _T.UNCERTAIN,
            }
        ),
        _T.PUBLISHED: frozenset({_T.VERIFIED}),
        _T.VERIFIED: frozenset(),
        # A TikTok inbox draft or the assisted checklist: the person finishes
        # it (published) or drops it (cancelled; assisted only, nothing sent).
        _T.NEEDS_USER: frozenset({_T.PUBLISHED, _T.FAILED_FINAL, _T.CANCELLED}),
        _T.FAILED_RETRYABLE: frozenset(
            {_T.QUEUED, _T.DRAFT, _T.READY, _T.RELEASED, _T.PUBLISHED, _T.CANCELLED}
        ),
        _T.FAILED_FINAL: frozenset({_T.DRAFT, _T.READY, _T.RELEASED, _T.PUBLISHED, _T.CANCELLED}),
        _T.UNCERTAIN: frozenset(
            {_T.PUBLISHED, _T.PROCESSING, _T.NEEDS_USER, _T.FAILED_RETRYABLE, _T.FAILED_FINAL}
        ),
        _T.CANCELLED: frozenset({_T.DRAFT, _T.READY}),
    },
    initial=frozenset({_T.DRAFT}),
    column="state",
)

# States in which nothing is in flight and an edit is allowed. Editing a
# released or queued target clears its release (spec 03 section 2).
TARGET_EDITABLE_STATES: frozenset[TargetState] = frozenset(
    {
        _T.DRAFT,
        _T.READY,
        _T.RELEASED,
        _T.QUEUED,
        _T.FAILED_RETRYABLE,
        _T.FAILED_FINAL,
        _T.CANCELLED,
    }
)
# States a person may cancel ("only before sending").
TARGET_CANCELLABLE_STATES: frozenset[TargetState] = frozenset(
    {_T.DRAFT, _T.READY, _T.RELEASED, _T.QUEUED, _T.FAILED_RETRYABLE, _T.FAILED_FINAL}
)
# States a release may start from: validated, or a failure a person retries.
TARGET_RELEASABLE_STATES: frozenset[TargetState] = frozenset(
    {_T.READY, _T.FAILED_RETRYABLE, _T.FAILED_FINAL}
)
# Settled states: nothing more will happen without a person.
TARGET_TERMINAL_STATES: frozenset[TargetState] = frozenset(
    {_T.VERIFIED, _T.FAILED_FINAL, _T.CANCELLED}
)

# Target states that count as "went out" when deriving a draft's status.
TARGET_DELIVERED_STATES: frozenset[TargetState] = frozenset({_T.PUBLISHED, _T.VERIFIED})

_A = PlatformActionState
PLATFORM_ACTION_MACHINE: StateMachine[PlatformActionState] = StateMachine(
    "platform action",
    PlatformActionState,
    {
        # Failing before anything was sent is a plain failure.
        _A.PROPOSED: frozenset({_A.ATTEMPTED, _A.FAILED}),
        _A.ATTEMPTED: frozenset({_A.COMPLETED, _A.UNCERTAIN, _A.FAILED}),
        _A.COMPLETED: frozenset(),
        _A.FAILED: frozenset(),
        # Only a reconciliation that re-read the platform may settle these;
        # an uncertain write is never retried automatically (invariant 4).
        _A.UNCERTAIN: frozenset({_A.COMPLETED, _A.FAILED}),
    },
    initial=frozenset({_A.PROPOSED}),
    column="state",
)

_C = CommentStatus
_ALL_COMMENT_STATUSES = frozenset(CommentStatus)
# Moderation labels are the owner's choice; any label may follow any other.
COMMENT_STATUS_MACHINE: StateMachine[CommentStatus] = StateMachine(
    "comment",
    CommentStatus,
    {status: _ALL_COMMENT_STATUSES - {status} for status in CommentStatus},
    initial=frozenset({_C.NEW}),
)


def derive_draft_status(
    current: DraftStatus | str,
    *,
    scheduled: bool,
    target_states: list[TargetState | str],
) -> DraftStatus:
    """The one rule for a draft's status from its schedule and targets.

    Archived drafts stay archived. Delivery only ever moves a draft forward
    (published targets cannot be un-sent), and an unsent draft is `scheduled`
    exactly when it has a planned time.
    """
    state = DRAFT_STATUS_MACHINE.parse(current)
    if state is DraftStatus.ARCHIVED:
        return state
    targets = [TARGET_STATE_MACHINE.parse(value) for value in target_states]
    delivered = sum(1 for target in targets if target in TARGET_DELIVERED_STATES)
    if targets and delivered == len(targets):
        return DraftStatus.PUBLISHED
    # Checked before the partial case: a published draft that gains an unsent
    # target stays published rather than moving back to partially_published.
    if state is DraftStatus.PUBLISHED:
        return state
    if delivered or state is DraftStatus.PARTIALLY_PUBLISHED:
        return DraftStatus.PARTIALLY_PUBLISHED
    return DraftStatus.SCHEDULED if scheduled else DraftStatus.DRAFT
