"""The "never both" rule between the Lineup browser publisher and the posts API.

A proposal's content may reach its channel's Community tab exactly once
(spec 08 section 2: "Never both for one proposal"). Two write paths exist:

- the browser publisher (`runway.publishing.youtube`), which schedules the
  proposal itself through a visible Chrome session;
- the `youtube_post` adapter (`runway.platforms.youtube_post`), which sends a
  post bridged from the proposal (`source='proposal'`, `source_ref=<id>`)
  through the YouTube `posts` API.

Each side reads the other path's claims through this module before it claims
the proposal for itself. "Claimed" means the platform may have received the
post or a submission is queued to send it; states that provably ended before
the point of no return do not block the other path. Channel identity is
compared conservatively: when either side's channel cannot be determined the
two are treated as the same channel (the owner install is single-channel).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.db.models import (
    ConnectedAccount,
    ContentDraft,
    DraftTarget,
    Proposal,
    PublishAttempt,
)
from runway.domain.enums import ProposalStatus, PublishAttemptStatus, TargetState
from runway.domain.state_machine import attempt_status_of
from runway.posts.types import PostSource, TargetPostType

# The shipped default of `Settings.publisher_channel_id`; a placeholder, not a
# real channel id, so it can never prove two paths post to different channels.
PLACEHOLDER_CHANNEL_ID = "UCxxxxxxxxxxxxxxxxxxxxxx"

# Proposal statuses meaning the browser path submitted (or is submitting) to
# YouTube. `internally_scheduled` alone is not a claim: a queued or prepared
# attempt under it is caught by the attempt check below.
_BROWSER_CLAIMED_STATUSES = frozenset(
    {
        ProposalStatus.PUBLISHING.value,
        ProposalStatus.EXTERNALLY_SCHEDULED.value,
        ProposalStatus.PUBLISH_UNVERIFIED.value,
        ProposalStatus.PUBLISHED.value,
    }
)

# Attempt states after which that attempt can no longer reach YouTube.
# Everything else - unfinished, submitted, and UNKNOWN - fails closed.
_BROWSER_FREE_ATTEMPT_STATUSES = frozenset(
    {
        PublishAttemptStatus.FAILED_BEFORE_SUBMISSION.value,
        PublishAttemptStatus.EXPIRED.value,
        PublishAttemptStatus.INVALIDATED.value,
        PublishAttemptStatus.SUPERSEDED.value,
    }
)

# Target states that provably sent nothing and will send nothing without
# another release: editing states, the cancelled end state, and the two
# pre-marker failures (`uncertain` is NOT here - the post may exist).
_API_UNCLAIMED_STATES = frozenset(
    {
        TargetState.DRAFT.value,
        TargetState.READY.value,
        TargetState.CANCELLED.value,
        TargetState.FAILED_RETRYABLE.value,
        TargetState.FAILED_FINAL.value,
    }
)


def channel_conflict(first: str | None, second: str | None) -> bool:
    """True unless both channel ids are known and provably different.

    Empty strings, None and the shipped `publisher_channel_id` placeholder
    all count as unknown, which fails closed to "same channel".
    """

    def known(value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped or stripped == PLACEHOLDER_CHANNEL_ID:
            return None
        return stripped

    a, b = known(first), known(second)
    if a is None or b is None:
        return True
    return a == b


def proposal_for_post(session: Session, post_id: int) -> Proposal | None:
    """The Lineup proposal a `source='proposal'` post was bridged from, if any."""
    post = session.get(ContentDraft, post_id)
    if post is None or post.source != PostSource.PROPOSAL.value or not post.source_ref:
        return None
    try:
        proposal_id = int(post.source_ref)
    except ValueError:
        return None
    return session.get(Proposal, proposal_id)


def browser_path_claimed(session: Session, proposal: Proposal) -> bool:
    """True when the browser publisher went (or may still go) out for this proposal.

    The claim holds while a submission could still reach YouTube: a claimed
    status, or any attempt that did not provably end before the submission
    boundary (UNKNOWN rows count as unfinished - see `attempt_blocks_new_work`).
    """
    if proposal.status in _BROWSER_CLAIMED_STATUSES:
        return True
    attempts = session.scalars(
        select(PublishAttempt).where(PublishAttempt.proposal_id == proposal.id)
    ).all()
    return any(
        attempt_status_of(attempt).value not in _BROWSER_FREE_ATTEMPT_STATUSES
        for attempt in attempts
    )


def claimed_api_targets(
    session: Session, proposal_id: int, *, exclude_target_id: int | None = None
) -> list[tuple[DraftTarget, ConnectedAccount | None]]:
    """Released-or-beyond `youtube_post` targets bridged from `proposal_id`.

    `draft`, `ready` and `cancelled` targets never went out and are not a
    claim; `failed_retryable`/`failed_final` provably reached the platform
    zero times (a post-marker failure settles as `uncertain`, which does
    claim). The account may be missing (deleted after release); those rows
    still count because their channel cannot be proven different.
    """
    statement = (
        select(DraftTarget, ConnectedAccount)
        .join(ContentDraft, DraftTarget.draft_id == ContentDraft.id)
        .outerjoin(ConnectedAccount, DraftTarget.account_id == ConnectedAccount.id)
        .where(
            ContentDraft.source == PostSource.PROPOSAL.value,
            ContentDraft.source_ref == str(proposal_id),
            DraftTarget.post_type == TargetPostType.YOUTUBE_POST.value,
            DraftTarget.state.not_in(sorted(_API_UNCLAIMED_STATES)),
        )
    )
    if exclude_target_id is not None:
        statement = statement.where(DraftTarget.id != exclude_target_id)
    return [(target, account) for target, account in session.execute(statement).all()]


def api_path_claimed(
    session: Session,
    proposal_id: int,
    *,
    channel_external_id: str | None,
    exclude_target_id: int | None = None,
) -> bool:
    """True when a posts-API `youtube_post` target claimed this proposal on a
    channel that cannot be proven different from `channel_external_id`."""
    return any(
        channel_conflict(
            account.external_account_id if account is not None else None,
            channel_external_id,
        )
        for _target, account in claimed_api_targets(
            session, proposal_id, exclude_target_id=exclude_target_id
        )
    )
