import pytest

from leeway.domain.enums import ProposalStatus
from leeway.domain.state_machine import InvalidTransition, require_transition, transition_allowed


def test_approval_boundary_is_explicit() -> None:
    assert transition_allowed(ProposalStatus.NEEDS_REVIEW, ProposalStatus.APPROVED)
    assert transition_allowed(ProposalStatus.APPROVED, ProposalStatus.INTERNALLY_SCHEDULED)
    assert not transition_allowed(ProposalStatus.NEEDS_REVIEW, ProposalStatus.INTERNALLY_SCHEDULED)


def test_published_proposal_is_terminal() -> None:
    with pytest.raises(InvalidTransition):
        require_transition(ProposalStatus.PUBLISHED, ProposalStatus.NEEDS_REVIEW)
