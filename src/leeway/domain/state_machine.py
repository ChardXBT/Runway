from __future__ import annotations

from dataclasses import dataclass

from leeway.domain.enums import ProposalStatus

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
        ProposalStatus.PUBLISHED,
        ProposalStatus.PUBLISH_FAILED,
    },
    ProposalStatus.PUBLISH_FAILED: {
        ProposalStatus.PUBLISHING,
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
