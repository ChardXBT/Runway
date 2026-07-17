from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import desc, select

from leeway.analysis.features import qlob_style_score
from leeway.analysis.service import AnalysisService, effective_annotation_fields
from leeway.captions.feedback import CaptionFeedbackService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import (
    MediaAsset,
    Post,
    PostAnnotation,
    PostMedia,
    Proposal,
    StyleProfile,
)
from leeway.media.service import cosine_similarity


class RetrievalService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def context_for_candidate(
        self,
        media_asset_id: int,
        *,
        candidate_id: int | None = None,
    ) -> dict[str, object]:
        with self.database.session() as session:
            candidate = session.get(MediaAsset, media_asset_id)
            if candidate is None or candidate.embedding_vector is None:
                raise LookupError(f"candidate media {media_asset_id} has no local embedding")
            profile_record = session.scalar(
                select(StyleProfile)
                .where(StyleProfile.is_active.is_(True))
                .order_by(desc(StyleProfile.version))
                .limit(1)
            )
            if profile_record is None:
                raise LookupError("build a style profile before retrieval")
            profile = json.loads(profile_record.profile_json)
            training_ids = [int(value) for value in profile["training_post_ids"]]
            rows = session.execute(
                select(Post, MediaAsset)
                .join(PostMedia, PostMedia.post_id == Post.id)
                .join(MediaAsset, MediaAsset.id == PostMedia.media_asset_id)
                .where(
                    Post.id.in_(training_ids),
                    Post.is_training_eligible.is_(True),
                    PostMedia.position == 0,
                )
            ).all()
            visual = sorted(
                (
                    cosine_similarity(candidate.embedding_vector, media.embedding_vector or b""),
                    post.id,
                    post,
                    media,
                )
                for post, media in rows
                if media.embedding_vector
            )
            visual_examples = [
                {
                    "post_id": post.id,
                    "caption": post.caption,
                    "published_at": post.published_at.isoformat() if post.published_at else None,
                    "visual_similarity": round(score, 6),
                    "media_asset_id": media.id,
                    "media_url": (
                        f"/media/{Path(media.local_path).relative_to('media').as_posix()}"
                    ),
                }
                for score, _post_id, post, media in reversed(visual[-8:])
            ]
            stats = profile["caption_statistics"]
            style_ranked = sorted(
                (
                    qlob_style_score(post.caption or "", stats),
                    post.id,
                    {"post_id": post.id, "caption": post.caption},
                )
                for post, _media in rows
            )
            caption_examples: list[dict[str, object]] = []
            seen_caption_ids: set[int] = set()
            for example in visual_examples[:4]:
                post_id = int(example["post_id"])
                caption_examples.append(
                    {
                        "post_id": post_id,
                        "caption": example["caption"],
                        "evidence": "visual_match",
                    }
                )
                seen_caption_ids.add(post_id)
            for score, post_id, item in reversed(style_ranked):
                if post_id in seen_caption_ids:
                    continue
                caption_examples.append(
                    {
                        **item,
                        "evidence": "channel_style",
                        "style_score": round(score, 6),
                    }
                )
                seen_caption_ids.add(post_id)
                if len(caption_examples) >= 8:
                    break
            cutoff = datetime.now(UTC) - timedelta(days=self.settings.duplicate_window_days)
            exclusions = []
            for post, media in rows:
                published = post.published_at
                if published is not None and published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
                if published is not None and published >= cutoff:
                    exclusions.append(
                        {
                            "post_id": post.id,
                            "media_asset_id": media.id,
                            "published_at": published.isoformat(),
                        }
                    )
                elif post.date_precision in {"relative", "unknown"}:
                    exclusions.append(
                        {
                            "post_id": post.id,
                            "media_asset_id": media.id,
                            "published_at": None,
                            "warning": "date precision cannot prove the 180-day boundary",
                        }
                    )
            annotations = session.scalars(
                select(PostAnnotation).where(
                    PostAnnotation.post_id.in_(training_ids),
                    PostAnnotation.annotation_version == AnalysisService.annotation_version,
                )
            ).all()
            effective_annotations = [
                effective_annotation_fields(annotation) for annotation in annotations
            ]
            franchise_rotation = Counter(
                str(annotation["franchise"] or "unknown") for annotation in effective_annotations
            )
            character_rotation: Counter[str] = Counter()
            for annotation in effective_annotations:
                character_rotation.update(
                    str(value) for value in cast(list[object], annotation["characters"])
                )
            negative = session.scalars(
                select(Proposal)
                .where(Proposal.status == "rejected")
                .order_by(desc(Proposal.rejected_at), desc(Proposal.id))
                .limit(5)
            ).all()
            feedback_context = (
                CaptionFeedbackService(self.database).context_for_candidate(candidate_id)
                if candidate_id is not None
                else {
                    "editorial_policy": {
                        "primary_goal": ("open-ended questions that invite community discussion"),
                        "recommended_structure": "open_question",
                    },
                    "positive_examples": [],
                    "negative_examples": [],
                    "learned_preferences": {},
                }
            )
            return {
                "visual_examples": visual_examples[:8],
                "caption_style_examples": caption_examples[:8],
                "recent_180_day_exclusions": exclusions,
                "rotation_state": {
                    "franchises": franchise_rotation.most_common(),
                    "characters": character_rotation.most_common(),
                },
                "negative_examples": [
                    {
                        "proposal_id": proposal.id,
                        "caption": proposal.final_caption,
                        "candidate_image_id": proposal.candidate_image_id,
                    }
                    for proposal in negative
                ],
                "style_profile": {
                    "summary": profile.get("summary", ""),
                    "caption_statistics": profile.get("caption_statistics", {}),
                    "dominant_caption_structures": profile.get("dominant_caption_structures", []),
                    "recent_overuse_rules": profile.get("recent_overuse_rules", {}),
                },
                "feedback_context": feedback_context,
                "style_profile_version": profile_record.version,
            }
