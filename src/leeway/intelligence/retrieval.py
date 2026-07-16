from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import desc, select

from leeway.analysis.features import qlob_style_score
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

    def context_for_candidate(self, media_asset_id: int) -> dict[str, object]:
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
            rows = session.execute(
                select(Post, MediaAsset)
                .join(PostMedia, PostMedia.post_id == Post.id)
                .join(MediaAsset, MediaAsset.id == PostMedia.media_asset_id)
                .where(Post.is_training_eligible.is_(True), PostMedia.position == 0)
            ).all()
            visual = sorted(
                (
                    cosine_similarity(candidate.embedding_vector, media.embedding_vector or b""),
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
                for score, post, media in reversed(visual[-8:])
            ]
            stats = profile["caption_statistics"]
            caption_examples = sorted(
                (
                    qlob_style_score(post.caption or "", stats),
                    post.id,
                    {"post_id": post.id, "caption": post.caption},
                )
                for post, _media in rows
            )
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
            annotations = session.scalars(select(PostAnnotation)).all()
            franchise_rotation = Counter(
                annotation.franchise or "unknown" for annotation in annotations
            )
            character_rotation: Counter[str] = Counter()
            for annotation in annotations:
                character_rotation.update(json.loads(annotation.characters_json))
            negative = session.scalars(
                select(Proposal)
                .where(Proposal.status == "rejected")
                .order_by(desc(Proposal.rejected_at), desc(Proposal.id))
                .limit(5)
            ).all()
            return {
                "visual_examples": visual_examples[:8],
                "caption_style_examples": [
                    item for _score, _post_id, item in reversed(caption_examples[-8:])
                ],
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
                "style_profile_version": profile_record.version,
            }
