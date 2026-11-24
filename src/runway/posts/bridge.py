""" "Also post to…": a post from an accepted Lineup proposal (spec 03 section 7).

One explicit action creates a post with `source='proposal'`,
`source_ref=<proposal id>`, the proposal's caption and a copy of its image,
and the targets the person picked. Nothing is created automatically and the
proposal itself (its Community-post path, status and attempts) is not touched.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from runway.auth.key_limits import acquire_cap_lock
from runway.auth.principal import POSTS_WRITE, Principal, require
from runway.auth.problems import Problem, ProblemCode
from runway.db.models import CandidateImage, Channel, MediaAsset, Proposal
from runway.domain.enums import ProposalStatus
from runway.posts.media_service import MediaService
from runway.posts.schemas import CrossPostRequest, PostCreate, PostOut
from runway.posts.service import PostService, post_view
from runway.posts.types import PostSource

# "Accepted into the Lineup": approved, or anything after it except cancelled.
ACCEPTED_PROPOSAL_STATUSES = frozenset(
    {
        ProposalStatus.APPROVED,
        ProposalStatus.INTERNALLY_SCHEDULED,
        ProposalStatus.PUBLISHING,
        ProposalStatus.EXTERNALLY_SCHEDULED,
        ProposalStatus.PUBLISH_UNVERIFIED,
        ProposalStatus.PUBLISH_FAILED,
        ProposalStatus.PUBLISHED,
    }
)
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


@dataclass
class ProposalBridge:
    posts: PostService
    media: MediaService

    def cross_post(self, principal: Principal, proposal_id: int, data: CrossPostRequest) -> PostOut:
        require(principal, POSTS_WRITE)
        database = self.posts.database
        with database.session() as session:
            agent = principal.kind == "agent"
            if agent:
                # The daily post cap is enforced in create_post_in below; the
                # lock that makes its check-then-act atomic must be taken
                # before this transaction's first statement.
                acquire_cap_lock(session, principal)
            proposal = session.get(Proposal, proposal_id)
            channel = session.get(Channel, proposal.channel_id) if proposal is not None else None
            if (
                proposal is None
                or channel is None
                or channel.workspace_id != (principal.workspace_id)
            ):
                raise Problem.of(ProblemCode.NOT_FOUND, "this proposal does not exist")
            if ProposalStatus(proposal.status) not in ACCEPTED_PROPOSAL_STATUSES:
                raise Problem.of(
                    ProblemCode.INVALID_STATE,
                    "only a proposal accepted into the Lineup can be posted elsewhere",
                )
            media_ids: list[int] = []
            candidate = session.get(CandidateImage, proposal.candidate_image_id)
            image = session.get(MediaAsset, candidate.media_asset_id) if candidate else None
            if image is not None:
                data_dir = self.posts.settings.resolved_data_dir.resolve()
                source = (data_dir / image.local_path).resolve()
                if not source.is_relative_to(data_dir) or not source.is_file():
                    raise Problem.of(
                        ProblemCode.VALIDATION_FAILED, "the proposal's image file is missing"
                    )
                content_type = _IMAGE_TYPES.get(source.suffix.lower()) or (
                    mimetypes.guess_type(source.name)[0] or image.mime_type
                )
                asset = self.media.import_file(
                    session,
                    workspace_id=principal.workspace_id,
                    source=source,
                    content_type=content_type,
                    filename=image.original_filename or Path(image.local_path).name,
                    created_by=f"{principal.kind}:{principal.id}",
                )
                media_ids.append(asset.id)
            caption = proposal.final_caption or proposal.recommended_caption
            post = self.posts.create_post_in(
                session,
                principal,
                PostCreate(
                    title=data.title or (caption[:80] or f"Proposal {proposal.id}"),
                    body=caption,
                    media_ids=media_ids,
                    targets=data.targets,
                    # A person's cross-post keeps the proposal's judgement:
                    # the Lineup image is a found photo, not synthetic media.
                    # A key's post leaves the label unset so the agent
                    # default (spec 06 section 4) applies, not a forced "no".
                    ai_generated=None if agent else False,
                ),
                # A key's cross-post is the key's own post: source 'agent' puts
                # its ready targets in the approval queue. A person's stays a
                # proposal cross-post, releasable like any human post.
                source=PostSource.AGENT if agent else PostSource.PROPOSAL,
                source_ref=str(proposal.id),
            )
            return post_view(session, self.posts.sources, post)
