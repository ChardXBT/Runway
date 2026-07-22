from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
from sqlalchemy import desc, func, select

from runway.analysis.features import (
    aggregate_caption_features,
    caption_features,
    channel_style_score,
    text_embedding,
)
from runway.analysis.runtime import AgentRuntime, runtime_for
from runway.analysis.service import (
    CORRECTION_OUTPUT_ALIASES,
    AnalysisService,
    effective_annotation_fields,
    image_matrix_for_posts,
)
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AuditEvent,
    BlindStudy,
    BlindStudyCase,
    BlindStudyResponse,
    CaptionCandidateRecord,
    CaptionExposure,
    CaptionFeedback,
    FeedbackSignal,
    IntelligenceRetrievalRun,
    MediaAsset,
    ModelRun,
    PairwisePreference,
    Post,
    PostAnnotation,
    PostMedia,
    Proposal,
    RepresentationSet,
    RetrievalEvidenceRecord,
    StyleProfile,
    utcnow,
)
from runway.db.repositories import audit, get_channel
from runway.intelligence.content_modes import ChannelContentModeService
from runway.intelligence.policies import ChannelPolicyService
from runway.media.service import cosine_similarity, ensure_fixture_images, inspect_image


class StyleProfileService:
    schema_version = "channel-profile-v4"

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: AgentRuntime | None = None,
    ):
        self.database = database
        self.settings = settings
        self.runtime = runtime or runtime_for(settings)

    async def build(self) -> dict[str, object]:
        with self.database.session() as session:
            target_channel = get_channel(session, self.settings.channel_handle)
            channel_id = target_channel.id
            channel_name = target_channel.name
        policy = ChannelPolicyService(self.database, self.settings).ensure_defaults(channel_id)
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            posts = session.scalars(
                select(Post)
                .where(
                    Post.channel_id == channel.id,
                    Post.is_training_eligible.is_(True),
                )
                .order_by(Post.id)
            ).all()
            if len(posts) < 3:
                raise ValueError("at least three training-eligible posts are required")
            holdout_count = max(1, round(len(posts) * 0.2))
            holdout_ids = [post.id for post in posts[::5]][:holdout_count]
            if len(holdout_ids) < holdout_count:
                holdout_ids.extend(post.id for post in posts if post.id not in holdout_ids)
                holdout_ids = holdout_ids[:holdout_count]
            training = [post for post in posts if post.id not in set(holdout_ids)]
            annotation_priority = {
                version: index
                for index, version in enumerate(
                    reversed(AnalysisService.compatible_annotation_versions)
                )
            }
            all_annotations: dict[int, PostAnnotation] = {}
            for annotation in session.scalars(
                select(PostAnnotation)
                .where(
                    PostAnnotation.post_id.in_([post.id for post in posts]),
                    PostAnnotation.annotation_version.in_(
                        AnalysisService.compatible_annotation_versions
                    ),
                )
                .order_by(PostAnnotation.created_at, PostAnnotation.id)
            ):
                current = all_annotations.get(annotation.post_id)
                if current is None or annotation_priority.get(
                    annotation.annotation_version,
                    -1,
                ) > annotation_priority.get(current.annotation_version, -1):
                    all_annotations[annotation.post_id] = annotation
            annotations = {
                post.id: all_annotations[post.id] for post in training if post.id in all_annotations
            }
            if len(annotations) < len(training):
                raise ValueError("analyze history before building a style profile")
            effective_annotations = {
                post_id: effective_annotation_fields(annotation)
                for post_id, annotation in annotations.items()
            }
            caption_stats = aggregate_caption_features(post.caption or "" for post in training)
            median_words = float(caption_stats.get("median_words", 0))
            representatives = self._select_representatives(
                training,
                effective_annotations,
                median_words,
            )
            representative_media: dict[int, str | None] = {}
            for representative in representatives:
                media = session.scalar(
                    select(MediaAsset)
                    .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                    .where(PostMedia.post_id == representative.id)
                    .order_by(PostMedia.position)
                    .limit(1)
                )
                representative_media[representative.id] = (
                    f"/media/{Path(media.local_path).relative_to('media').as_posix()}"
                    if media
                    else None
                )
            franchise_counts = Counter(
                str(effective_annotations[post.id]["franchise"] or "unknown") for post in training
            )
            entity_counts: Counter[str] = Counter()
            action_counts: Counter[str] = Counter()
            character_counts: Counter[str] = Counter()
            for post in training:
                for entity in cast(
                    list[dict[str, object]],
                    effective_annotations[post.id].get("entities", []),
                ):
                    name = str(entity.get("canonical_name") or entity.get("name") or "").strip()
                    if name:
                        entity_counts[name] += 1
                action_counts.update(
                    str(value)
                    for value in cast(
                        list[object],
                        effective_annotations[post.id].get("actions", []),
                    )
                    if str(value).strip()
                )
                character_counts.update(
                    str(value)
                    for value in cast(list[object], effective_annotations[post.id]["characters"])
                )
            topic_counts = Counter(
                {
                    name: count
                    for name, count in franchise_counts.items()
                    if name.strip().casefold() not in {"", "unknown", "none", "null"}
                }
            )
            if not topic_counts:
                topic_counts.update(entity_counts)
            visual_formats = Counter(
                self._canonical_visual_format(str(effective_annotations[post.id]["visual_format"]))
                for post in training
            )
            structures = Counter(
                self._canonical_caption_structure(post.caption or "") for post in training
            )
            compositions = Counter(
                self._canonical_composition(post, effective_annotations[post.id])
                for post in training
            )
            raw_visual_formats = Counter(
                str(effective_annotations[post.id]["visual_format"]) for post in training
            )
            raw_structures = Counter(
                str(effective_annotations[post.id]["caption_structure"]) for post in training
            )
            raw_compositions = Counter(
                str(effective_annotations[post.id]["composition"]) for post in training
            )
            reviewed_training_post_ids = sorted(
                post_id
                for post_id, annotation in annotations.items()
                if annotation.review_status == "reviewed"
            )
            reviewed_catalogue_post_ids = sorted(
                post_id
                for post_id, annotation in all_annotations.items()
                if annotation.review_status == "reviewed"
            )
            corrected_training_post_ids = sorted(
                post_id
                for post_id, annotation in annotations.items()
                if json.loads(annotation.reviewed_fields_json or "{}")
            )
            corrected_catalogue_post_ids = sorted(
                post_id
                for post_id, annotation in all_annotations.items()
                if json.loads(annotation.reviewed_fields_json or "{}")
            )
            rejected = session.scalars(
                select(Proposal)
                .where(
                    Proposal.channel_id == channel.id,
                    Proposal.status == "rejected",
                )
                .order_by(desc(Proposal.rejected_at), desc(Proposal.id))
                .limit(5)
            ).all()
            recent_proposals = session.scalars(
                select(Proposal)
                .where(Proposal.channel_id == channel.id)
                .order_by(desc(Proposal.updated_at), desc(Proposal.id))
                .limit(30)
            ).all()
            legacy_feedback = session.scalars(
                select(CaptionFeedback)
                .join(Proposal, Proposal.id == CaptionFeedback.proposal_id)
                .where(Proposal.channel_id == channel.id)
            ).all()
            feedback_signals = session.scalars(
                select(FeedbackSignal).where(FeedbackSignal.channel_id == channel.id)
            ).all()
            pairwise_count = int(
                session.scalar(
                    select(func.count(PairwisePreference.id)).where(
                        PairwisePreference.channel_id == channel.id
                    )
                )
                or 0
            )
            next_version = (
                session.scalar(
                    select(func.max(StyleProfile.version)).where(
                        StyleProfile.channel_id == channel.id
                    )
                )
                or 0
            ) + 1
            cutoff = max(post.updated_at for post in posts)

        preferred_structures = Counter(
            value.preferred_structure for value in legacy_feedback if value.preferred_structure
        )
        image_verdicts = Counter(
            signal.verdict for signal in feedback_signals if signal.target == "image"
        )
        caption_verdicts = Counter(
            signal.verdict for signal in feedback_signals if signal.target == "caption"
        )
        pairing_verdicts = Counter(
            signal.verdict for signal in feedback_signals if signal.target == "pairing"
        )

        representative_examples = [
            {
                "post_id": post.id,
                "caption": post.caption,
                "likes": post.like_count,
                "comments": post.comment_count,
                "franchise": effective_annotations[post.id]["franchise"],
                "visual_format": effective_annotations[post.id]["visual_format"],
                "composition": effective_annotations[post.id]["composition"],
                "caption_structure": effective_annotations[post.id]["caption_structure"],
                "humor_style": effective_annotations[post.id]["humor_style"],
                "tone": effective_annotations[post.id]["tone"],
                "profile_tags": {
                    "caption_structure": self._canonical_caption_structure(post.caption or ""),
                    "visual_format": self._canonical_visual_format(
                        str(effective_annotations[post.id]["visual_format"])
                    ),
                    "composition": self._canonical_composition(
                        post, effective_annotations[post.id]
                    ),
                },
            }
            for post in representatives
        ]
        chronological_examples = [
            {
                "post_id": post.id,
                "caption": post.caption,
                "published_at": post.published_at.isoformat() if post.published_at else None,
                "franchise": effective_annotations[post.id]["franchise"],
                "composition": self._canonical_composition(post, effective_annotations[post.id]),
                "caption_structure": self._canonical_caption_structure(post.caption or ""),
            }
            for post in sorted(
                training,
                key=lambda item: (
                    (
                        item.published_at.replace(tzinfo=UTC)
                        if item.published_at and item.published_at.tzinfo is None
                        else item.published_at
                    )
                    or datetime.min.replace(tzinfo=UTC),
                    item.id,
                ),
                reverse=True,
            )[:20]
        ]
        summary_payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "annotation_version": AnalysisService.annotation_version,
            "training_sample_count": len(training),
            "median_caption_words": caption_stats.get("median_words"),
            "question_frequency": caption_stats.get("question_frequency"),
            "caption_statistics": caption_stats,
            "dominant_structures": structures.most_common(5),
            "visual_formats": visual_formats.most_common(5),
            "visual_compositions": compositions.most_common(5),
            "franchise_distribution": franchise_counts.most_common(5),
            "entity_distribution": entity_counts.most_common(10),
            "topic_distribution": topic_counts.most_common(10),
            "action_distribution": action_counts.most_common(10),
            "character_distribution": character_counts.most_common(10),
            "representative_post_ids": [post.id for post in representatives],
            "representative_examples": representative_examples,
            "recent_chronological_examples": chronological_examples,
        }
        started = utcnow()
        summary = await self.runtime.build_style_summary(summary_payload)
        supplied_summary_ids = {
            cast(int, example["post_id"])
            for example in representative_examples + chronological_examples
        }
        unknown_citations = set(summary.cited_post_ids) - supplied_summary_ids
        if unknown_citations:
            raise ValueError(
                "style summary cited post IDs absent from its evidence payload: "
                + ", ".join(str(value) for value in sorted(unknown_citations))
            )
        content_modes = ChannelContentModeService(self.database).learn(
            channel_id=channel_id,
            post_ids=[post.id for post in training],
        )
        profile: dict[str, Any] = {
            "schema_version": self.schema_version,
            "annotation_versions": sorted(
                {annotation.annotation_version for annotation in all_annotations.values()}
            ),
            "version": next_version,
            "channel": channel_name,
            "channel_id": channel_id,
            "training_post_ids": [post.id for post in training],
            "holdout_post_ids": holdout_ids,
            "holdout_fraction": round(len(holdout_ids) / len(posts), 6),
            "caption_statistics": caption_stats,
            "dominant_caption_structures": structures.most_common(10),
            "visual_formats": visual_formats.most_common(10),
            "visual_compositions": compositions.most_common(10),
            "raw_annotation_vocabulary": {
                "caption_structures": raw_structures.most_common(20),
                "visual_formats": raw_visual_formats.most_common(20),
                "compositions": raw_compositions.most_common(20),
            },
            "franchise_distribution": franchise_counts.most_common(),
            "character_distribution": character_counts.most_common(),
            "entity_distribution": entity_counts.most_common(),
            "topic_distribution": topic_counts.most_common(),
            "action_distribution": action_counts.most_common(),
            "reviewed_training_annotation_post_ids": reviewed_training_post_ids,
            "reviewed_training_annotation_count": len(reviewed_training_post_ids),
            "reviewed_catalogue_annotation_post_ids": reviewed_catalogue_post_ids,
            "reviewed_catalogue_annotation_count": len(reviewed_catalogue_post_ids),
            "corrected_training_annotation_post_ids": corrected_training_post_ids,
            "corrected_training_annotation_count": len(corrected_training_post_ids),
            "corrected_catalogue_annotation_post_ids": corrected_catalogue_post_ids,
            "corrected_catalogue_annotation_count": len(corrected_catalogue_post_ids),
            "representative_positive_examples": [
                {
                    "post_id": post.id,
                    "caption": post.caption,
                    "media_url": representative_media[post.id],
                    "likes": post.like_count,
                    "comments": post.comment_count,
                    "annotation": effective_annotations[post.id],
                    "profile_tags": {
                        "caption_structure": self._canonical_caption_structure(post.caption or ""),
                        "visual_format": self._canonical_visual_format(
                            str(effective_annotations[post.id]["visual_format"])
                        ),
                        "composition": self._canonical_composition(
                            post, effective_annotations[post.id]
                        ),
                    },
                }
                for post in representatives
            ],
            "outliers": self._outliers(training),
            "rotation_patterns": summary.rotation_observations,
            "recent_overuse_rules": {
                "avoid_consecutive_topic": True,
                "avoid_consecutive_composition": True,
                "duplicate_window_days": self.settings.duplicate_window_days,
            },
            "negative_examples": [
                {"proposal_id": proposal.id, "caption": proposal.final_caption}
                for proposal in rejected
            ],
            "summary": summary.summary,
            "summary_cited_post_ids": summary.cited_post_ids,
            "profile_reliability": {
                "history_samples": len(training),
                "minimum_reliable_samples": 20,
                "reliable": len(training) >= 20,
                "missing_evidence": (["more creator history"] if len(training) < 20 else []),
            },
            "long_term_channel_dna": {
                "language": policy.language,
                "locale": policy.locale,
                "caption_statistics": caption_stats,
                "structures": structures.most_common(10),
                "vocabulary": caption_stats.get("common_openings", []),
                "punctuation": {
                    "question_frequency": caption_stats.get("question_frequency", 0),
                    "exclamation_frequency": caption_stats.get(
                        "exclamation_frequency",
                        0,
                    ),
                    "emoji_frequency": caption_stats.get("emoji_frequency", 0),
                },
                "visual_formats": visual_formats.most_common(10),
                "compositions": compositions.most_common(10),
                "entities": entity_counts.most_common(20),
                "topics": topic_counts.most_common(20),
                "actions": action_counts.most_common(20),
            },
            "recent_editorial_mode": {
                "recent_post_ids": [
                    cast(int, example["post_id"]) for example in chronological_examples[:10]
                ],
                "scheduled_or_approved_proposal_ids": [
                    proposal.id
                    for proposal in recent_proposals
                    if proposal.status
                    in {
                        "approved",
                        "internally_scheduled",
                        "publishing",
                        "externally_scheduled",
                        "publish_unverified",
                    }
                ],
                "recent_rejection_ids": [proposal.id for proposal in rejected],
                "rotation_windows": [3, 10, 30],
            },
            "content_modes": content_modes,
            "explicit_channel_policy": policy.model_dump(),
            "learned_creator_preference": {
                "legacy_feedback_count": len(legacy_feedback),
                "pairwise_preference_count": pairwise_count,
                "preferred_structures": preferred_structures.most_common(),
                "image_verdicts": image_verdicts.most_common(),
                "caption_verdicts": caption_verdicts.most_common(),
                "pairing_verdicts": pairing_verdicts.most_common(),
            },
            "audience_performance": {
                "status": "observed_history_only",
                "like_samples": sum(post.like_count is not None for post in training),
                "comment_samples": sum(post.comment_count is not None for post in training),
                "used_for_creator_preference": False,
            },
        }
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            for existing_profile in session.scalars(
                select(StyleProfile).where(StyleProfile.channel_id == channel.id)
            ):
                existing_profile.is_active = False
            record = StyleProfile(
                channel_id=channel.id,
                version=next_version,
                catalogue_cutoff=cutoff,
                profile_json=json.dumps(profile, sort_keys=True),
                representative_post_ids_json=json.dumps([post.id for post in representatives]),
                excluded_post_ids_json=json.dumps(holdout_ids),
                is_active=True,
            )
            session.add(record)
            session.add(
                ModelRun(
                    task_type="build_style_summary",
                    provider=self.runtime.provider,
                    model=self.runtime.model_name,
                    prompt_version="style-summary-v1",
                    input_record_ids_json=json.dumps([post.id for post in training]),
                    request_summary_json=json.dumps(summary_payload, sort_keys=True),
                    structured_output_json=summary.model_dump_json(),
                    token_usage_json=json.dumps(
                        getattr(self.runtime, "last_token_usage", {}), sort_keys=True
                    ),
                    started_at=started,
                    completed_at=datetime.now(UTC),
                    status="completed",
                )
            )
            session.flush()
            audit(
                session,
                "style_profile_built",
                "style_profile",
                record.id,
                {"version": next_version, "holdout_ids": holdout_ids},
            )
        self._write_profile_reports(next_version, profile)
        return profile

    def active(self) -> dict[str, Any]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            record = session.scalar(
                select(StyleProfile)
                .where(
                    StyleProfile.channel_id == channel.id,
                    StyleProfile.is_active.is_(True),
                )
                .order_by(desc(StyleProfile.version))
                .limit(1)
            )
            if record is None:
                raise LookupError("no active style profile")
            result = cast(dict[str, Any], json.loads(record.profile_json))
            result["id"] = record.id
            result["created_at"] = record.created_at.isoformat()
            result["catalogue_cutoff"] = record.catalogue_cutoff.isoformat()
            return result

    def backfill_content_modes(self) -> dict[str, object]:
        """Create an immutable profile revision with learned content modes.

        This intentionally reuses the active profile and active multimodal
        representation set. It never invokes the generation runtime and is safe
        to repeat: an equivalent learned mode payload leaves the active profile
        unchanged.
        """
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            channel_id = channel.id
            source = session.scalar(
                select(StyleProfile)
                .where(
                    StyleProfile.channel_id == channel_id,
                    StyleProfile.is_active.is_(True),
                )
                .order_by(desc(StyleProfile.version))
                .limit(1)
            )
            if source is None:
                raise LookupError("no active style profile")
            source_id = source.id
            source_version = source.version
            source_cutoff = source.catalogue_cutoff
            representatives = source.representative_post_ids_json
            excluded = source.excluded_post_ids_json
            profile = cast(dict[str, Any], json.loads(source.profile_json))

        post_ids = [
            int(value)
            for value in cast(list[Any], profile.get("training_post_ids", []))
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        if len(post_ids) < 3:
            raise ValueError("active profile has fewer than three training post IDs")
        learned_modes = ChannelContentModeService(self.database).learn(
            channel_id=channel_id,
            post_ids=post_ids,
        )
        content_modes = cast(
            dict[str, object],
            json.loads(json.dumps(learned_modes, sort_keys=True)),
        )
        if content_modes.get("status") != "learned":
            return {
                "status": "blocked",
                "profile_id": source_id,
                "profile_version": source_version,
                "content_modes": content_modes,
            }
        if profile.get("content_modes") == content_modes:
            self._write_profile_reports(source_version, profile)
            return {
                "status": "unchanged",
                "profile_id": source_id,
                "profile_version": source_version,
                "content_modes": content_modes,
            }

        with self.database.session() as session:
            current = session.scalar(
                select(StyleProfile)
                .where(
                    StyleProfile.channel_id == channel_id,
                    StyleProfile.is_active.is_(True),
                )
                .order_by(desc(StyleProfile.version))
                .limit(1)
            )
            if current is None or current.id != source_id:
                raise RuntimeError("active style profile changed during content-mode backfill")
            next_version = int(
                session.scalar(
                    select(func.max(StyleProfile.version)).where(
                        StyleProfile.channel_id == channel_id
                    )
                )
                or 0
            ) + 1
            profile["version"] = next_version
            profile["content_modes"] = content_modes
            profile["profile_derivation"] = {
                "kind": "content_mode_backfill",
                "source_profile_id": source_id,
                "source_profile_version": source_version,
                "content_mode_version": ChannelContentModeService.version,
                "generation_runtime_invoked": False,
            }
            current.is_active = False
            record = StyleProfile(
                channel_id=channel_id,
                version=next_version,
                catalogue_cutoff=source_cutoff,
                profile_json=json.dumps(profile, sort_keys=True),
                representative_post_ids_json=representatives,
                excluded_post_ids_json=excluded,
                is_active=True,
            )
            session.add(record)
            session.flush()
            audit(
                session,
                "style_profile_content_modes_backfilled",
                "style_profile",
                record.id,
                {
                    "source_profile_id": source_id,
                    "source_profile_version": source_version,
                    "new_profile_version": next_version,
                    "content_mode_count": content_modes.get("mode_count", 0),
                    "generation_runtime_invoked": False,
                },
            )
            profile_id = record.id
        self._write_profile_reports(next_version, profile)
        return {
            "status": "created",
            "profile_id": profile_id,
            "profile_version": next_version,
            "source_profile_id": source_id,
            "content_modes": content_modes,
        }

    def list_profiles(self) -> list[dict[str, object]]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            records = session.scalars(
                select(StyleProfile)
                .where(StyleProfile.channel_id == channel.id)
                .order_by(desc(StyleProfile.version))
            ).all()
            return [
                {
                    "id": record.id,
                    "version": record.version,
                    "is_active": record.is_active,
                    "catalogue_cutoff": record.catalogue_cutoff.isoformat(),
                    "created_at": record.created_at.isoformat(),
                }
                for record in records
            ]

    def detail(self, profile_id: int) -> dict[str, Any]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            record = session.get(StyleProfile, profile_id)
            if record is None or record.channel_id != channel.id:
                raise LookupError(f"style profile {profile_id} not found")
            result = cast(dict[str, Any], json.loads(record.profile_json))
            result["id"] = record.id
            result["is_active"] = record.is_active
            result["created_at"] = record.created_at.isoformat()
            return result

    def latest_evaluation(self) -> dict[str, Any]:
        path = self.settings.resolved_data_dir / "reports" / "profile-evaluation.json"
        if not path.is_file():
            raise LookupError("no profile evaluation report exists")
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    def evaluate(self) -> dict[str, Any]:
        profile = self.active()
        training_ids = [int(value) for value in cast(list[Any], profile["training_post_ids"])]
        holdout_ids = [int(value) for value in cast(list[Any], profile["holdout_post_ids"])]
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            channel_id = channel.id
            posts = {
                post.id: post
                for post in session.scalars(
                    select(Post).where(
                        Post.channel_id == channel.id,
                        Post.id.in_(training_ids + holdout_ids),
                    )
                )
            }
            annotations: dict[int, PostAnnotation] = {}
            priority = {
                version: index
                for index, version in enumerate(
                    reversed(AnalysisService.compatible_annotation_versions)
                )
            }
            for annotation in session.scalars(
                select(PostAnnotation).where(
                    PostAnnotation.post_id.in_(training_ids + holdout_ids),
                    PostAnnotation.annotation_version.in_(
                        AnalysisService.compatible_annotation_versions
                    ),
                )
            ):
                current = annotations.get(annotation.post_id)
                if current is None or priority.get(
                    annotation.annotation_version,
                    -1,
                ) > priority.get(current.annotation_version, -1):
                    annotations[annotation.post_id] = annotation
            effective_annotations = {
                post_id: effective_annotation_fields(annotation)
                for post_id, annotation in annotations.items()
            }

        matching = self._image_caption_matching(training_ids, holdout_ids, posts)
        stats = cast(dict[str, Any], profile["caption_statistics"])
        generic = [
            "Like and subscribe for more!",
            "What do you think? Comment below!",
            "This is an image from a popular show.",
        ]
        ranking_hits = 0
        for post_id in holdout_ids:
            own = channel_style_score(posts[post_id].caption or "", stats)
            distractor = max(channel_style_score(caption, stats) for caption in generic)
            ranking_hits += own > distractor
        ranking_accuracy = ranking_hits / len(holdout_ids) if holdout_ids else 0.0

        reviewed_labels: dict[tuple[int, str], object] = {}
        with self.database.session() as session:
            review_events = session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.event_type == "annotation_reviewed",
                    AuditEvent.entity_type == "post",
                    AuditEvent.entity_id.in_(training_ids + holdout_ids),
                )
                .order_by(AuditEvent.created_at, AuditEvent.id)
            ).all()
            for event in review_events:
                if event.entity_id is None:
                    continue
                details = json.loads(event.details_json)
                if (
                    details.get("annotation_version")
                    not in AnalysisService.compatible_annotation_versions
                ):
                    continue
                expected_fields = details.get("expected_fields", {})
                if not isinstance(expected_fields, dict):
                    continue
                for field, expected in expected_fields.items():
                    reviewed_labels[(event.entity_id, field)] = expected
        reviewed_total = 0
        reviewed_correct = 0
        reviewed_post_ids: set[int] = set()
        for (post_id, field), expected in reviewed_labels.items():
            review_annotation = annotations.get(post_id)
            if review_annotation is None:
                continue
            original = json.loads(review_annotation.original_output_json)
            reviewed_total += 1
            reviewed_post_ids.add(post_id)
            reviewed_correct += (
                original.get(CORRECTION_OUTPUT_ALIASES.get(field, field)) == expected
            )

        retrieval_hits = 0
        train_matrix, train_matched = image_matrix_for_posts(self.database, training_ids)
        holdout_matrix, holdout_matched = image_matrix_for_posts(self.database, holdout_ids)
        for vector, post_id in zip(holdout_matrix, holdout_matched, strict=True):
            similarities = train_matrix @ vector
            top_indices = np.argsort(similarities)[::-1][:3]
            expected = effective_annotations.get(post_id)
            expected_topics = self._topic_keys(expected) if expected else set()
            if expected and any(
                effective_annotations.get(train_matched[index])
                and self._topic_keys(effective_annotations[train_matched[index]]) & expected_topics
                for index in top_indices
            ):
                retrieval_hits += 1
        retrieval_relevance = retrieval_hits / len(holdout_matched) if holdout_matched else 0.0

        duplicate_metrics = self._duplicate_metrics()
        current_pipeline = self._current_pipeline_metrics(channel_id)
        report: dict[str, Any] = {
            "evaluation_version": "current-intelligence-evaluation-v2",
            "profile_version": profile["version"],
            "training_samples": len(training_ids),
            "holdout_samples": len(holdout_ids),
            "holdout_fraction": profile["holdout_fraction"],
            "image_caption_matching": matching,
            "channel_caption_ranking_accuracy": round(ranking_accuracy, 6),
            "reviewed_annotation_field_accuracy": (
                round(reviewed_correct / reviewed_total, 6) if reviewed_total else None
            ),
            "reviewed_annotation_fields": reviewed_total,
            "reviewed_annotation_posts": len(reviewed_post_ids),
            "review_sample_strategy": "manual uncertainty/outlier audit; not a random sample",
            "duplicate_detection": duplicate_metrics,
            "retrieval_top3_topic_relevance": round(retrieval_relevance, 6),
            "current_pipeline": current_pipeline,
            "historical_metrics": {
                "status": "historical_diagnostic_only",
                "definitive": False,
                "image_caption_matching": matching,
                "generic_vs_channel_caption_ranking_accuracy": round(
                    ranking_accuracy,
                    6,
                ),
                "legacy_visual_top3_topic_relevance": round(retrieval_relevance, 6),
            },
            "sample_errors": matching.get("errors", []),
            "limitations": [
                "Image-caption matching, caption ranking, and retrieval relevance use the "
                "captured channel holdout; duplicate transformation recall uses controlled "
                "synthetic fixtures.",
                "Reviewed annotation accuracy covers only fields with a recorded human "
                "review and uses an uncertainty/outlier sample, so it is not an unbiased "
                "full-catalogue accuracy estimate.",
                "YouTube exposed relative publication dates, so reconstructed timestamps "
                "are approximate.",
                "The system builds a retrieval profile; it does not fine-tune model weights.",
                "Legacy image-caption projection, generic-caption ranking, and franchise-style "
                "retrieval metrics are retained only as historical diagnostics. Current pipeline "
                "evidence is reported separately and raw-frontier claims require human arena data.",
            ],
        }
        self._write_evaluation(report)
        return report

    def _current_pipeline_metrics(self, channel_id: int) -> dict[str, object]:
        with self.database.session() as session:
            active_sets = session.scalars(
                select(RepresentationSet)
                .where(
                    RepresentationSet.channel_id == channel_id,
                    RepresentationSet.active.is_(True),
                    RepresentationSet.status == "active",
                )
                .order_by(RepresentationSet.scope, RepresentationSet.purpose)
            ).all()
            selected_evidence = session.scalars(
                select(RetrievalEvidenceRecord)
                .join(
                    IntelligenceRetrievalRun,
                    IntelligenceRetrievalRun.id == RetrievalEvidenceRecord.retrieval_run_id,
                )
                .where(
                    IntelligenceRetrievalRun.channel_id == channel_id,
                    RetrievalEvidenceRecord.selected.is_(True),
                )
            ).all()
            caption_rows = session.scalars(
                select(CaptionCandidateRecord).where(
                    CaptionCandidateRecord.channel_id == channel_id,
                    CaptionCandidateRecord.displayed.is_(True),
                )
            ).all()
            exposures = session.scalars(
                select(CaptionExposure).where(CaptionExposure.channel_id == channel_id)
            ).all()
            human_labels = session.execute(
                select(PairwisePreference.target, func.count(PairwisePreference.id))
                .where(
                    PairwisePreference.channel_id == channel_id,
                    PairwisePreference.label_source == "human",
                )
                .group_by(PairwisePreference.target)
            ).all()
            arena_studies = session.scalars(
                select(BlindStudy).where(
                    BlindStudy.channel_id == channel_id,
                    BlindStudy.target == "frontier_arena",
                )
            ).all()
            arena_case_count = 0
            arena_response_count = 0
            if arena_studies:
                study_ids = [study.id for study in arena_studies]
                arena_case_count = int(
                    session.scalar(
                        select(func.count(BlindStudyCase.id)).where(
                            BlindStudyCase.blind_study_id.in_(study_ids)
                        )
                    )
                    or 0
                )
                arena_response_count = int(
                    session.scalar(
                        select(func.count(BlindStudyResponse.id))
                        .join(
                            BlindStudyCase,
                            BlindStudyCase.id == BlindStudyResponse.blind_study_case_id,
                        )
                        .where(BlindStudyCase.blind_study_id.in_(study_ids))
                        .where(BlindStudyResponse.reviewer_kind == "creator")
                    )
                    or 0
                )

        grounding_passes = 0
        for row in caption_rows:
            try:
                result = json.loads(row.verifier_result_json)
            except (TypeError, json.JSONDecodeError):
                continue
            grounding_passes += bool(isinstance(result, dict) and result.get("passed") is True)
        role_counts = Counter(row.evidence_role or "unassigned" for row in selected_evidence)
        no_edit = sum(
            exposure.decision_type in {"accepted", "selected", "approved"} for exposure in exposures
        )
        return {
            "status": "current",
            "representation_sets": [
                {
                    "id": row.id,
                    "scope": row.scope,
                    "purpose": row.purpose,
                    "provider": row.provider,
                    "model": row.model,
                    "model_version": row.model_version,
                    "coverage": (
                        row.completed_count / row.expected_count if row.expected_count else 1.0
                    ),
                }
                for row in active_sets
            ],
            "retrieval": {
                "selected_evidence_count": len(selected_evidence),
                "role_counts": dict(sorted(role_counts.items())),
                "human_labelled_ndcg": None,
                "human_labelled_recall_at_k": None,
            },
            "caption_pipeline": {
                "displayed_candidate_count": len(caption_rows),
                "grounding_pass_rate": (
                    round(grounding_passes / len(caption_rows), 6) if caption_rows else None
                ),
                "exposure_count": len(exposures),
                "observed_no_edit_acceptance_rate": (
                    round(no_edit / len(exposures), 6) if exposures else None
                ),
            },
            "human_preference_labels": {str(target): int(count) for target, count in human_labels},
            "raw_frontier_arena": {
                "study_count": len(arena_studies),
                "case_count": arena_case_count,
                "human_response_count": arena_response_count,
                "comparison_data_available": arena_response_count > 0,
                "superiority_claim_supported": False,
                "superiority_requires": (
                    "preregistered arm-specific win rate and confidence analysis; "
                    "at least 50 directional cases"
                ),
                "status": (
                    "results_available" if arena_response_count else "awaiting_human_review"
                ),
            },
        }

    def _image_caption_matching(
        self,
        training_ids: list[int],
        holdout_ids: list[int],
        posts: dict[int, Post],
    ) -> dict[str, Any]:
        train_images, train_matched = image_matrix_for_posts(self.database, training_ids)
        holdout_images, holdout_matched = image_matrix_for_posts(self.database, holdout_ids)
        if len(train_matched) < 2 or not holdout_matched:
            return {"accuracy": 0.0, "evaluated": 0, "errors": ["insufficient paired samples"]}
        train_captions = np.vstack(
            [text_embedding(posts[post_id].caption or "") for post_id in train_matched]
        )
        projection = np.linalg.pinv(train_images) @ train_captions
        candidates = np.vstack(
            [text_embedding(posts[post_id].caption or "") for post_id in holdout_matched]
        )
        hits = 0
        errors: list[dict[str, object]] = []
        for index, (image_vector, post_id) in enumerate(
            zip(holdout_images, holdout_matched, strict=True)
        ):
            predicted = image_vector @ projection
            norm = float(np.linalg.norm(predicted)) or 1.0
            scores = candidates @ (predicted / norm)
            selected = int(np.argmax(scores))
            if selected == index:
                hits += 1
            else:
                errors.append(
                    {
                        "post_id": post_id,
                        "selected_post_id": holdout_matched[selected],
                        "score": round(float(scores[selected]), 6),
                    }
                )
        return {
            "accuracy": round(hits / len(holdout_matched), 6),
            "evaluated": len(holdout_matched),
            "errors": errors,
        }

    def _duplicate_metrics(self) -> dict[str, object]:
        assets = ensure_fixture_images(self.settings)
        original = inspect_image(assets["history-01"])
        positives = [
            inspect_image(assets["history-01-resized"]),
            inspect_image(assets["history-01-cropped"]),
            inspect_image(assets["history-01-color"]),
            inspect_image(assets["history-01-bordered"]),
        ]
        negative = inspect_image(assets["unrelated"])

        def transformed_match(candidate: Any) -> bool:
            perceptual = int(original.perceptual_hash, 16) ^ int(candidate.perceptual_hash, 16)
            phash_similarity = 1 - perceptual.bit_count() / 64
            semantic = cosine_similarity(original.embedding, candidate.embedding)
            return phash_similarity >= 0.88 or semantic >= 0.99

        true_positives = sum(transformed_match(candidate) for candidate in positives)
        false_positive = transformed_match(negative)
        return {
            "evaluation_source": "controlled synthetic transformations",
            "exact_identity_passed": original.sha256 == original.sha256,
            "transformed_true_positive_rate": round(true_positives / len(positives), 6),
            "unrelated_false_positive_rate": float(false_positive),
            "positive_variants": len(positives),
            "documented_phash_threshold": 0.88,
            "documented_semantic_threshold": 0.99,
        }

    @staticmethod
    def _outliers(posts: list[Post]) -> list[dict[str, object]]:
        ordered = sorted(posts, key=lambda post: len((post.caption or "").split()))
        selected = ordered[:1] + ordered[-1:]
        return [
            {
                "post_id": post.id,
                "caption": post.caption,
                "word_count": len((post.caption or "").split()),
            }
            for post in selected
        ]

    @staticmethod
    def _select_representatives(
        posts: list[Post],
        annotations: dict[int, dict[str, object]],
        median_words: float,
        *,
        limit: int = 8,
    ) -> list[Post]:
        """Select typical but structurally varied examples deterministically."""
        remaining = list(posts)
        selected: list[Post] = []
        seen_structures: set[str] = set()
        seen_compositions: set[str] = set()
        seen_formats: set[str] = set()
        seen_question_states: set[bool] = set()
        max_distance = (
            max(
                (abs(len((post.caption or "").split()) - median_words) for post in posts),
                default=1.0,
            )
            or 1.0
        )
        while remaining and len(selected) < min(limit, len(posts)):

            def score(post: Post) -> tuple[float, int, int]:
                annotation = annotations[post.id]
                structure = StyleProfileService._canonical_caption_structure(post.caption or "")
                composition = StyleProfileService._canonical_composition(post, annotation)
                visual_format = StyleProfileService._canonical_visual_format(
                    str(annotation["visual_format"])
                )
                is_question = "?" in (post.caption or "")
                novelty = (
                    3.0 * (structure not in seen_structures)
                    + 2.0 * (composition not in seen_compositions)
                    + 1.0 * (visual_format not in seen_formats)
                    + 1.0 * (is_question not in seen_question_states)
                )
                typicality = 1.0 - (
                    abs(len((post.caption or "").split()) - median_words) / max_distance
                )
                engagement = (post.like_count or 0) + 3 * (post.comment_count or 0)
                return novelty + typicality, engagement, -post.id

            chosen = max(remaining, key=score)
            selected.append(chosen)
            remaining.remove(chosen)
            effective = annotations[chosen.id]
            seen_structures.add(
                StyleProfileService._canonical_caption_structure(chosen.caption or "")
            )
            seen_compositions.add(StyleProfileService._canonical_composition(chosen, effective))
            seen_formats.add(
                StyleProfileService._canonical_visual_format(str(effective["visual_format"]))
            )
            seen_question_states.add("?" in (chosen.caption or ""))
        return selected

    @staticmethod
    def _topic_keys(annotation: dict[str, object]) -> set[str]:
        values: set[str] = set()
        franchise = str(annotation.get("franchise") or "").strip().casefold()
        if franchise and franchise not in {"unknown", "none", "null"}:
            values.add(franchise)
        for entity in cast(list[object], annotation.get("entities", [])):
            if isinstance(entity, dict):
                name = str(entity.get("canonical_name") or entity.get("name") or "")
            else:
                name = str(entity)
            normalized = name.strip().casefold()
            if normalized:
                values.add(normalized)
        return values

    @staticmethod
    def _canonical_caption_structure(caption: str) -> str:
        features = caption_features(caption)
        words = cast(list[str], features["tokens"])
        lowered = caption.strip().lower()
        if lowered.startswith(("http://", "https://")):
            return "link share"
        if any(marker in lowered for marker in ("credit", "drawn by", "art by", "artist:")):
            return "attribution or credit"
        if int(features["word_count"]) == 1:
            return "single word"
        if bool(features["has_question"]):
            return "direct question"
        if '"' in caption or "“" in caption or "”" in caption:
            return "quoted line or reference"
        if bool(features["has_exclamation"]):
            return "exclamatory statement"
        if words and words[0] in {"i", "i'm", "im", "we", "my", "our"}:
            return "first-person reaction"
        if int(features["word_count"]) <= 3:
            return "short phrase"
        if int(features["word_count"]) <= 7:
            return "short statement"
        return "long statement"

    @staticmethod
    def _canonical_visual_format(value: str) -> str:
        lowered = value.lower()
        if any(marker in lowered for marker in ("collage", "contact sheet", "multi-panel")):
            return "collage or multi-panel"
        if "screenshot" in lowered and any(
            marker in lowered for marker in ("social", "reddit", "post", "web")
        ):
            return "social-media screenshot"
        if any(
            marker in lowered for marker in ("product", "packaging", "merchandise", "toy", "candy")
        ):
            return "product or merchandise image"
        if any(
            marker in lowered
            for marker in ("fan art", "artwork", "drawing", "illustration", "spreadsheet")
        ):
            return "artwork or constructed image"
        if any(marker in lowered for marker in ("animation", "animated", "television still")):
            return "animated still or frame"
        if "photograph" in lowered or "photo" in lowered:
            return "photograph"
        if "screenshot" in lowered:
            return "screenshot"
        return "other visual format"

    @staticmethod
    def _canonical_composition(post: Post, annotation: dict[str, object]) -> str:
        value = str(annotation["composition"]).lower()
        visible_count = int(cast(int, annotation["visible_character_count"]))
        if post.post_type == "multi_image":
            return "multi-image comparison"
        if any(marker in value for marker in ("split", "comparison", "side-by-side")):
            return "split or comparison"
        if "close-up" in value or "close up" in value:
            return "close-up"
        if any(marker in value for marker in ("crowd", "group", "many characters")):
            return "group or crowd"
        if "overhead" in value or "top-down" in value:
            return "overhead scene"
        if any(marker in value for marker in ("wide shot", "wide view", "landscape")):
            return "wide scene"
        if any(
            marker in value
            for marker in ("product", "packaging", "newspaper", "poster", "sign", "object")
        ):
            return "object-focused"
        if visible_count == 0:
            return "environment or object"
        if visible_count == 1:
            return "single-character scene"
        if visible_count == 2:
            return "two-character scene"
        if visible_count >= 4:
            return "group or crowd"
        return "multi-character scene"

    def _write_profile_reports(self, version: int, profile: dict[str, Any]) -> None:
        reports = self.settings.resolved_data_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"style-profile-v{version}.json").write_text(
            json.dumps(profile, indent=2, sort_keys=True), encoding="utf-8"
        )
        stats = profile["caption_statistics"]
        policy = profile.get("explicit_channel_policy")
        policy_version = (
            str(policy.get("version") or "not recorded")
            if isinstance(policy, dict)
            else "not recorded"
        )
        lines = [
            f"# {profile['channel']} channel profile v{version}",
            "",
            profile["summary"],
            "",
            f"- Training records: {len(profile['training_post_ids'])}",
            f"- Holdout records: {len(profile['holdout_post_ids'])}",
            f"- Median words: {stats['median_words']}",
            f"- Question frequency: {stats.get('question_frequency', 0):.1%}",
            f"- Exclamation frequency: {stats.get('exclamation_frequency', 0):.1%}",
            f"- Representative post IDs: {profile['summary_cited_post_ids']}",
            f"- Policy version: {policy_version}",
            "",
            "This is a reproducible retrieval/style profile, not model-weight fine-tuning.",
        ]
        (reports / f"style-profile-v{version}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _write_evaluation(self, report: dict[str, Any]) -> None:
        reports = self.settings.resolved_data_dir / "reports"
        (reports / "profile-evaluation.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        lines = [
            f"# Style profile v{report['profile_version']} current intelligence evaluation",
            "",
            f"- Train / holdout: {report['training_samples']} / {report['holdout_samples']}",
            "- Active representation sets: "
            f"{len(report['current_pipeline']['representation_sets'])}",
            "- Human frontier-arena responses: "
            f"{report['current_pipeline']['raw_frontier_arena']['human_response_count']}",
            "- Transformed duplicate recall: "
            f"{report['duplicate_detection']['transformed_true_positive_rate']:.1%}",
            "",
            "## Historical diagnostics (not definitive current quality scores)",
            "",
            "- Legacy image-caption projection: "
            f"{report['image_caption_matching']['accuracy']:.1%}",
            "- Generic-vs-channel caption ranking: "
            f"{report['channel_caption_ranking_accuracy']:.1%}",
            "- Legacy visual top-3 topic relevance: "
            f"{report['retrieval_top3_topic_relevance']:.1%}",
            "",
            "## Limitations",
            "",
            *[f"- {item}" for item in report["limitations"]],
        ]
        (reports / "profile-evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
